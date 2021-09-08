"""
The MIT License (MIT)

Copyright (c) 2021-present Pycord Development

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Union

from ..enums import SlashCommandOptionType
from ..member import Member
from ..user import User
from ..message import Message
from .context import InteractionContext
from ..utils import find, get_or_fetch, async_all
from ..errors import DiscordException, NotFound, ValidationError, ClientException
from .errors import ApplicationCommandError, CheckFailure, ApplicationCommandInvokeError

__all__ = (
    "ApplicationCommand",
    "SlashCommand",
    "Option",
    "OptionChoice",
    "option",
    "SubCommandGroup",
    "ContextMenuCommand",
    "UserCommand",
    "MessageCommand",
    "command",
    "application_command",
    "slash_command",
    "user_command",
    "message_command",
)

def wrap_callback(coro):
    @functools.wraps(coro)
    async def wrapped(*args, **kwargs):
        try:
            ret = await coro(*args, **kwargs)
        except ApplicationCommandError:
            raise
        except asyncio.CancelledError:
            return
        except Exception as exc:
            raise ApplicationCommandInvokeError(exc) from exc
        return ret
    return wrapped

def hooked_wrapped_callback(command, ctx, coro):
    @functools.wraps(coro)
    async def wrapped(arg):
        try:
            ret = await coro(arg)
        except ApplicationCommandError:
            raise
        except asyncio.CancelledError:
            return
        except Exception as exc:
            raise ApplicationCommandInvokeError(exc) from exc
        finally:
            await command.call_after_hooks(ctx)
        return ret
    return wrapped

class ApplicationCommand:
    def __repr__(self):
        return f"<discord.app.commands.{self.__class__.__name__} name={self.name}>"

    def __eq__(self, other):
        return isinstance(other, self.__class__)

    async def prepare(self, ctx: InteractionContext) -> None:
        # This should be same across all 3 types
        ctx.command = self

        if not await self.can_run(ctx):
            raise CheckFailure(f'The check functions for the command {self.name} failed')

        # TODO: Add cooldown

        await self.call_before_hooks(ctx)
        pass

    async def invoke(self, ctx: InteractionContext) -> None:
        await self.prepare(ctx)

        injected = hooked_wrapped_callback(self, ctx, self._invoke)
        await injected(ctx)

    async def can_run(self, ctx: InteractionContext) -> bool:

        if not await ctx.bot.can_run(ctx):
            raise CheckFailure(f'The global check functions for command {self.name} failed.')

        predicates = self.checks
        if not predicates:
            # since we have no checks, then we just return True.
            return True

        return await async_all(predicate(ctx) for predicate in predicates) # type: ignore    
    
    async def dispatch_error(self, ctx: InteractionContext, error: Exception) -> None:
        ctx.command_failed = True
        cog = self.cog
        try:
            coro = self.on_error
        except AttributeError:
            pass
        else:
            injected = wrap_callback(coro)
            if cog is not None:
                await injected(cog, ctx, error)
            else:
                await injected(ctx, error)

        try:
            if cog is not None:
                local = cog.__class__._get_overridden_method(cog.cog_command_error)
                if local is not None:
                    wrapped = wrap_callback(local)
                    await wrapped(ctx, error)
        finally:
            ctx.bot.dispatch('application_command_error', ctx, error)

    def _get_signature_parameters(self):
        return OrderedDict(inspect.signature(self.callback).parameters)

    def error(self, coro):
        """A decorator that registers a coroutine as a local error handler.

        A local error handler is an :func:`.on_command_error` event limited to
        a single command. However, the :func:`.on_command_error` is still
        invoked afterwards as the catch-all.

        Parameters
        -----------
        coro: :ref:`coroutine <coroutine>`
            The coroutine to register as the local error handler.

        Raises
        -------
        TypeError
            The coroutine passed is not actually a coroutine.
        """

        if not asyncio.iscoroutinefunction(coro):
            raise TypeError('The error handler must be a coroutine.')

        self.on_error = coro
        return coro

    def has_error_handler(self) -> bool:
        """:class:`bool`: Checks whether the command has an error handler registered.
        """
        return hasattr(self, 'on_error')

    def before_invoke(self, coro):
        """A decorator that registers a coroutine as a pre-invoke hook.
        A pre-invoke hook is called directly before the command is
        called. This makes it a useful function to set up database
        connections or any type of set up required.
        This pre-invoke hook takes a sole parameter, a :class:`.Context`.
        See :meth:`.Bot.before_invoke` for more info.
        Parameters
        -----------
        coro: :ref:`coroutine <coroutine>`
            The coroutine to register as the pre-invoke hook.
        Raises
        -------
        TypeError
            The coroutine passed is not actually a coroutine.
        """
        if not asyncio.iscoroutinefunction(coro):
            raise TypeError('The pre-invoke hook must be a coroutine.')

        self._before_invoke = coro
        return coro

    def after_invoke(self, coro):
        """A decorator that registers a coroutine as a post-invoke hook.
        A post-invoke hook is called directly after the command is
        called. This makes it a useful function to clean-up database
        connections or any type of clean up required.
        This post-invoke hook takes a sole parameter, a :class:`.Context`.
        See :meth:`.Bot.after_invoke` for more info.
        Parameters
        -----------
        coro: :ref:`coroutine <coroutine>`
            The coroutine to register as the post-invoke hook.
        Raises
        -------
        TypeError
            The coroutine passed is not actually a coroutine.
        """
        if not asyncio.iscoroutinefunction(coro):
            raise TypeError('The post-invoke hook must be a coroutine.')

        self._after_invoke = coro
        return coro

    async def call_before_hooks(self, ctx: InteractionContext) -> None:
        # now that we're done preparing we can call the pre-command hooks
        # first, call the command local hook:
        cog = self.cog
        if self._before_invoke is not None:
            # should be cog if @commands.before_invoke is used
            instance = getattr(self._before_invoke, '__self__', cog)
            # __self__ only exists for methods, not functions
            # however, if @command.before_invoke is used, it will be a function
            if instance:
                await self._before_invoke(instance, ctx)  # type: ignore
            else:
                await self._before_invoke(ctx)  # type: ignore

        # call the cog local hook if applicable:
        if cog is not None:
            hook = cog.__class__._get_overridden_method(cog.cog_before_invoke)
            if hook is not None:
                await hook(ctx)

        # call the bot global hook if necessary
        hook = ctx.bot._before_invoke
        if hook is not None:
            await hook(ctx)

    async def call_after_hooks(self, ctx: InteractionContext) -> None:
        cog = self.cog
        if self._after_invoke is not None:
            instance = getattr(self._after_invoke, '__self__', cog)
            if instance:
                await self._after_invoke(instance, ctx)  # type: ignore
            else:
                await self._after_invoke(ctx)  # type: ignore

        # call the cog local hook if applicable:
        if cog is not None:
            hook = cog.__class__._get_overridden_method(cog.cog_after_invoke)
            if hook is not None:
                await hook(ctx)

        hook = ctx.bot._after_invoke
        if hook is not None:
            await hook(ctx)

class SlashCommand(ApplicationCommand):
    type = 1

    def __new__(cls, *args, **kwargs) -> SlashCommand:
        self = super().__new__(cls)

        self.__original_kwargs__ = kwargs.copy()
        return self

    def __init__(self, func: Callable, *args, **kwargs) -> None:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Callback must be a coroutine.")
        self.callback = func

        self.guild_ids: Optional[List[int]] = kwargs.get("guild_ids", None)

        name = kwargs.get("name") or func.__name__
        validate_chat_input_name(name)
        self.name: str = name

        description = kwargs.get("description") or (
            inspect.cleandoc(func.__doc__).splitlines()[0]
            if func.__doc__ is not None
            else "No description provided"
        )
        validate_chat_input_description(description)
        self.description: str = description
        self.is_subcommand: bool = False
        self.cog = None

        params = self._get_signature_parameters()
        self.options = self.parse_options(params)

        try:
            checks = func.__commands_checks__
            checks.reverse()
        except AttributeError:
            checks = kwargs.get('checks', [])

        self.checks = checks

        self._before_invoke = None
        self._after_invoke = None
        

    def parse_options(self, params) -> List[Option]:
        params = iter(params.items())
        final_options = []

        if self.cog is not None:
            # we have 'self' as the first parameter so just advance
            # the iterator and resume parsing
            try:
                next(params)
            except StopIteration:
                raise ClientException(
                    f'Callback for {self.name} command is missing "self" parameter.'
                )

        # next we have the 'ctx' as the next parameter
        try:
            next(params)
        except StopIteration:
            raise ClientException(
                f'Callback for {self.name} command is missing "ctx" parameter.'
            )

        final_options = []

        for p_name, p_obj in params:

            option = p_obj.annotation
            if option == inspect.Parameter.empty:
                option = str

            if self._is_typing_optional(option):
                option = Option(
                    option.__args__[0], "No description provided", required=False
                )

            if not isinstance(option, Option):
                option = Option(option, "No description provided")
                if p_obj.default != inspect.Parameter.empty:
                    option.required = False

            option.default = option.default or p_obj.default

            if option.default == inspect.Parameter.empty:
                option.default = None

            if option.name is None:
                option.name = p_name

            final_options.append(option)

        return final_options

    def _is_typing_optional(self, annotation):
        return getattr(annotation, "__origin__", None) is Union and type(None) in annotation.__args__  # type: ignore

    def to_dict(self) -> Dict:
        as_dict = {
            "name": self.name,
            "description": self.description,
            "options": [o.to_dict() for o in self.options],
        }
        if self.is_subcommand:
            as_dict["type"] = SlashCommandOptionType.sub_command.value

        return as_dict

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, SlashCommand)
            and other.name == self.name
            and other.description == self.description
        )

    async def _invoke(self, ctx: InteractionContext) -> None:
        # TODO: Parse the args better, apply custom converters etc.
        kwargs = {}
        for arg in ctx.interaction.data.get("options", []):
            op = find(lambda x: x.name == arg["name"], self.options)
            arg = arg["value"]

            # Checks if input_type is user, role or channel
            if (
                SlashCommandOptionType.user.value
                <= op.input_type.value
                <= SlashCommandOptionType.role.value
            ):
                name = "member" if op.input_type.name == "user" else op.input_type.name
                arg = await get_or_fetch(ctx.guild, name, int(arg))

            elif op.input_type == SlashCommandOptionType.mentionable:
                try:
                    arg = await get_or_fetch(ctx.guild, "member", int(arg))
                except NotFound:
                    arg = await get_or_fetch(ctx.guild, "role", int(arg))

            kwargs[op.name] = arg

        for o in self.options:
            if o.name not in kwargs:
                kwargs[o.name] = o.default
        await self.callback(ctx, **kwargs)

class Option:
    def __init__(
        self, input_type: SlashCommandOptionType, /, description = None,**kwargs
    ) -> None:
        self.name: Optional[str] = kwargs.pop("name", None)
        self.description = description or "No description provided"
        if not isinstance(input_type, SlashCommandOptionType):
            input_type = SlashCommandOptionType.from_datatype(input_type)
        self.input_type = input_type
        self.required: bool = kwargs.pop("required", True)
        self.choices: List[OptionChoice] = [
            o if isinstance(o, OptionChoice) else OptionChoice(o)
            for o in kwargs.pop("choices", list())
        ]
        self.default = kwargs.pop("default", None)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "type": self.input_type.value,
            "required": self.required,
            "choices": [c.to_dict() for c in self.choices],
        }

    def __repr__(self):
        return f"<discord.app.commands.{self.__class__.__name__} name={self.name}>"


class OptionChoice:
    def __init__(self, name: str, value: Optional[Union[str, int, float]] = None):
        self.name = name
        self.value = value or name

    def to_dict(self) -> Dict[str, Union[str, int, float]]:
        return {"name": self.name, "value": self.value}

def option(name, type, **kwargs):
    """A decorator that can be used instead of typehinting Option"""
    def decor(func):
        func.__annotations__[name] = Option(type, **kwargs)
        return func
    return decor

class SubCommandGroup(ApplicationCommand, Option):
    type = 1

    def __new__(cls, *args, **kwargs) -> SubCommandGroup:
        self = super().__new__(cls)

        self.__original_kwargs__ = kwargs.copy()
        return self

    def __init__(
        self,
        name: str,
        description: str,
        guild_ids: Optional[List[int]] = None,
        parent_group: Optional[SubCommandGroup] = None,
    ) -> None:
        validate_chat_input_name(name)
        validate_chat_input_description(description)
        super().__init__(
            SlashCommandOptionType.sub_command_group,
            name=name,
            description=description,
        )
        self.subcommands: List[Union[SlashCommand, SubCommandGroup]] = []
        self.guild_ids = guild_ids
        self.parent_group = parent_group
        self.checks = []

        self._before_invoke = None
        self._after_invoke = None
        self.cog = None

    def to_dict(self) -> Dict:
        as_dict = {
            "name": self.name,
            "description": self.description,
            "options": [c.to_dict() for c in self.subcommands],
        }

        if self.parent_group is not None:
            as_dict["type"] = self.input_type.value

        return as_dict

    def command(self, **kwargs) -> SlashCommand:
        def wrap(func) -> SlashCommand:
            command = SlashCommand(func, **kwargs)
            command.is_subcommand = True
            self.subcommands.append(command)
            return command

        return wrap

    def command_group(self, name, description) -> SubCommandGroup:
        if self.parent_group is not None:
            # TODO: Improve this error message
            raise Exception("Subcommands can only be nested once")

        sub_command_group = SubCommandGroup(name, description, parent_group=self)
        self.subcommands.append(sub_command_group)
        return sub_command_group

    async def _invoke(self, ctx: InteractionContext) -> None:
        option = ctx.interaction.data["options"][0]
        command = find(lambda x: x.name == option["name"], self.subcommands)
        ctx.interaction.data = option
        await command.invoke(ctx)


class ContextMenuCommand(ApplicationCommand):
    def __new__(cls, *args, **kwargs) -> ContextMenuCommand:
        self = super().__new__(cls)

        self.__original_kwargs__ = kwargs.copy()
        return self

    def __init__(self, func: Callable, *args, **kwargs) -> None:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Callback must be a coroutine.")
        self.callback = func

        self.guild_ids: Optional[List[int]] = kwargs.get("guild_ids", None)

        # Discord API doesn't support setting descriptions for User commands
        # so it must be empty
        self.description = ""
        self.name: str = kwargs.pop("name", func.__name__)
        if not isinstance(self.name, str):
            raise TypeError("Name of a command must be a string.")

        self.cog = None

        try:
            checks = func.__commands_checks__
            checks.reverse()
        except AttributeError:
            checks = kwargs.get('checks', [])

        self.checks = checks
        self._before_invoke = None
        self._after_invoke = None
        
        self.validate_parameters()

    def validate_parameters(self):
        params = iter(self._get_signature_parameters().items())
        if self.cog is not None:
            # we have 'self' as the first parameter so just advance
            # the iterator and resume parsing
            try:
                next(params)
            except StopIteration:
                raise ClientException(
                    f'Callback for {self.name} command is missing "self" parameter.'
                )

        # next we have the 'ctx' as the next parameter
        try:
            next(params)
        except StopIteration:
            raise ClientException(
                f'Callback for {self.name} command is missing "ctx" parameter.'
            )

        # next we have the 'user/message' as the next parameter
        try:
            next(params)
        except StopIteration:
            cmd = "user" if type(self) == UserCommand else "message"
            raise ClientException(
                f'Callback for {self.name} command is missing "{cmd}" parameter.'
            )

        # next there should be no more parameters
        try:
            next(params)
            raise ClientException(
                f"Callback for {self.name} command has too many parameters."
            )
        except StopIteration:
            pass

    def to_dict(self) -> Dict[str, Union[str, int]]:
        return {"name": self.name, "description": self.description, "type": self.type}


class UserCommand(ContextMenuCommand):
    type = 2

    def __new__(cls, *args, **kwargs) -> UserCommand:
        self = super().__new__(cls)

        self.__original_kwargs__ = kwargs.copy()
        return self

    async def _invoke(self, ctx: InteractionContext) -> None:
        if "members" not in ctx.interaction.data["resolved"]:
            _data = ctx.interaction.data["resolved"]["users"]
            for i, v in _data.items():
                v["id"] = int(i)
                user = v
            target = User(state=ctx.interaction._state, data=user)
        else:
            _data = ctx.interaction.data["resolved"]["members"]
            for i, v in _data.items():
                v["id"] = int(i)
                member = v
            _data = ctx.interaction.data["resolved"]["users"]
            for i, v in _data.items():
                v["id"] = int(i)
                user = v
            member["user"] = user
            target = Member(
                data=member,
                guild=ctx.interaction._state._get_guild(ctx.interaction.guild_id),
                state=ctx.interaction._state,
            )
        await self.callback(ctx, target)


class MessageCommand(ContextMenuCommand):
    type = 3

    def __new__(cls, *args, **kwargs) -> MessageCommand:
        self = super().__new__(cls)

        self.__original_kwargs__ = kwargs.copy()
        return self

    async def _invoke(self, ctx: InteractionContext):
        _data = ctx.interaction.data["resolved"]["messages"]
        for i, v in _data.items():
            v["id"] = int(i)
            message = v
        channel = ctx.interaction._state.get_channel(int(message["channel_id"]))
        if channel is None:
            data = await ctx.interaction._state.http.start_private_message(
                int(message["author"]["id"])
            )
            channel = ctx.interaction._state.add_dm_channel(data)

        target = Message(state=ctx.interaction._state, channel=channel, data=message)
        await self.callback(ctx, target)

def slash_command(**kwargs):
    """Decorator for slash commands that invokes :func:`application_command`.
    .. versionadded:: 2.0
    Returns
    --------
    Callable[..., :class:`SlashCommand`]
        A decorator that converts the provided method into a :class:`.SlashCommand`.
    """
    return application_command(cls=SlashCommand, **kwargs)

def user_command(**kwargs):
    """Decorator for user commands that invokes :func:`application_command`.
    .. versionadded:: 2.0
    Returns
    --------
    Callable[..., :class:`UserCommand`]
        A decorator that converts the provided method into a :class:`.UserCommand`.
    """
    return application_command(cls=UserCommand, **kwargs)

def message_command(**kwargs):
    """Decorator for message commands that invokes :func:`application_command`.
    .. versionadded:: 2.0
    Returns
    --------
    Callable[..., :class:`MessageCommand`]
        A decorator that converts the provided method into a :class:`.MessageCommand`.
    """
    return application_command(cls=MessageCommand, **kwargs)

def application_command(cls=SlashCommand, **attrs):
    """A decorator that transforms a function into an :class:`.ApplicationCommand`. More specifically,
    usually one of :class:`.SlashCommand`, :class:`.UserCommand`, or :class:`.MessageCommand`. The exact class
    depends on the ``cls`` parameter.
    By default the ``description`` attribute is received automatically from the
    docstring of the function and is cleaned up with the use of
    ``inspect.cleandoc``. If the docstring is ``bytes``, then it is decoded
    into :class:`str` using utf-8 encoding.
    The ``name`` attribute also defaults to the function name unchanged.
    .. versionadded:: 2.0
    Parameters
    -----------
    cls: :class:`.ApplicationCommand`
        The class to construct with. By default this is :class:`.SlashCommand`.
        You usually do not change this.
    attrs
        Keyword arguments to pass into the construction of the class denoted
        by ``cls``.
    Raises
    -------
    TypeError
        If the function is not a coroutine or is already a command.
    """

    def decorator(func: Callable) -> cls:
        if isinstance(func, ApplicationCommand):
            func = func.callback
        elif not callable(func):
            raise TypeError(
                "func needs to be a callable or a subclass of ApplicationCommand."
            )

        return cls(func, **attrs)

    return decorator

def command(**kwargs):
    """There is an alias for :meth:`application_command`.
    .. note::
        This decorator is overriden by :func:`commands.command`.
    .. versionadded:: 2.0
    Returns
    --------
    Callable[..., :class:`ApplicationCommand`]
        A decorator that converts the provided method into an :class:`.ApplicationCommand`.
    """
    return application_command(**kwargs)

# Validation
def validate_chat_input_name(name: Any):
    if not isinstance(name, str):
        raise TypeError("Name of a command must be a string.")
    if " " in name:
        raise ValidationError("Name of a chat input command cannot have spaces.")
    if not name.islower():
        raise ValidationError("Name of a chat input command must be lowercase.")
    if len(name) > 32 or len(name) < 1:
        raise ValidationError(
            "Name of a chat input command must be less than 32 characters and non empty."
        )


def validate_chat_input_description(description: Any):
    if not isinstance(description, str):
        raise TypeError("Description of a command must be a string.")
    if len(description) > 100 or len(description) < 1:
        raise ValidationError(
            "Description of a chat input command must be less than 100 characters and non empty."
        )
