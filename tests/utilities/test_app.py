from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from math import ceil
from pathlib import Path
from time import monotonic
from typing import AsyncContextManager, cast, ContextManager
from unittest import mock

from rich.console import Console

from textual import events
from textual.app import App, ComposeResult
from textual._context import active_app
from textual.driver import Driver
from textual.geometry import Size

if sys.version_info >= (3, 8):
    from typing import Protocol
else:
    from typing_extensions import Protocol


# N.B. These classes would better be named TestApp/TestConsole/TestDriver/etc,
# but it makes pytest emit warning as it will try to collect them as classes containing test cases :-/

# This value is also hard-coded in Textual's `App` class:
CLEAR_SCREEN_SEQUENCE = "\x1bP=1s\x1b\\"


class MockedTimeMoveClockForward(Protocol):
    async def __call__(self, *, seconds: float) -> tuple[float, int]:
        """Returns the new current (mocked) monotonic time and the number of activated Timers"""
        ...


class AppTest(App):
    def __init__(
        self,
        *,
        test_name: str,
        size: Size,
        log_verbosity: int = 2,
    ):
        # Tests will log in "/tests/test.[test name].log":
        log_path = Path(__file__).parent.parent / f"test.{test_name}.log"
        super().__init__(
            driver_class=DriverTest,
            log_path=log_path,
            log_verbosity=log_verbosity,
            log_color_system="256",
        )

        # Let's disable all features by default
        self.features = frozenset()

        # We need this so the `CLEAR_SCREEN_SEQUENCE` is always sent for a screen refresh,
        # whatever the environment:
        self._sync_available = True

        self._size = size
        self._console = ConsoleTest(width=size.width, height=size.height)
        self._error_console = ConsoleTest(width=size.width, height=size.height)

    def log_tree(self) -> None:
        """Handy shortcut when testing stuff"""
        self.log(self.tree)

    def compose(self) -> ComposeResult:
        raise NotImplementedError(
            "Create a subclass of TestApp and override its `compose()` method, rather than using TestApp directly"
        )

    def in_running_state(
        self,
        *,
        time_mocking_ticks_granularity_fps: int = 60,  # i.e. when moving forward by 1 second we'll do it though 60 ticks
        waiting_duration_after_initialisation: float = 0.1,
        waiting_duration_after_yield: float = 0,
    ) -> AsyncContextManager[MockedTimeMoveClockForward]:
        async def run_app() -> None:
            await self.process_messages()

        @contextlib.asynccontextmanager
        async def get_running_state_context_manager():
            with mock_textual_timers(
                ticks_granularity_fps=time_mocking_ticks_granularity_fps
            ) as move_clock_forward:
                run_task = asyncio.create_task(run_app())
                self._set_active()
                # Let's give some time to various asyncio-bound stuff to do their stuff:
                # timeout_before_yielding_task = asyncio.create_task(
                #     asyncio.sleep(0.1)
                # )
                # done, pending = await asyncio.wait(
                #     (
                #         run_task,
                #         timeout_before_yielding_task,
                #     ),
                #     return_when=asyncio.FIRST_COMPLETED,
                # )
                # if run_task in done or run_task not in pending:
                #     raise RuntimeError(
                #         "TestApp is no longer running after its initialization period"
                #     )

                # await asyncio.sleep(0.01)

                await move_clock_forward(seconds=1)

                await move_clock_forward(seconds=waiting_duration_after_initialisation)
                assert self._driver is not None

                await self.force_screen_update()

                yield move_clock_forward

                await move_clock_forward(seconds=waiting_duration_after_yield)

                await self.force_screen_update()

                assert not run_task.done()
            await self.shutdown()

        return get_running_state_context_manager()

    async def boot_and_shutdown(
        self,
        *,
        waiting_duration_after_initialisation: float = 0.001,
        waiting_duration_before_shutdown: float = 0,
    ):
        """Just a commodity shortcut for `async with app.in_running_state(): pass`, for simple cases"""
        async with self.in_running_state(
            waiting_duration_after_initialisation=waiting_duration_after_initialisation,
            waiting_duration_after_yield=waiting_duration_before_shutdown,
        ):
            pass

    async def force_screen_update(self, *, repaint: bool = True, layout: bool = True) -> None:
        try:
            screen = self.screen
        except IndexError:
            return  # the app may not have a screen yet
        screen.refresh(repaint=repaint, layout=layout)
        screen._on_update()

        await asyncio.sleep(0.001)

    def on_exception(self, error: Exception) -> None:
        # In tests we want the errors to be raised, rather than printed to a Console
        raise error

    def run(self):
        raise NotImplementedError(
            "Use `async with my_test_app.in_running_state()` rather than `my_test_app.run()`"
        )

    @property
    def active_app(self) -> App | None:
        return active_app.get()

    @property
    def total_capture(self) -> str | None:
        return self.console.file.getvalue()

    @property
    def last_display_capture(self) -> str | None:
        total_capture = self.total_capture
        if not total_capture:
            return None
        last_display_start_index = total_capture.rindex(CLEAR_SCREEN_SEQUENCE)
        return total_capture[last_display_start_index:]

    @property
    def console(self) -> ConsoleTest:
        return self._console

    @console.setter
    def console(self, console: Console) -> None:
        """This is a no-op, the console is always a TestConsole"""
        return

    @property
    def error_console(self) -> ConsoleTest:
        return self._error_console

    @error_console.setter
    def error_console(self, console: Console) -> None:
        """This is a no-op, the error console is always a TestConsole"""
        return


class ConsoleTest(Console):
    def __init__(self, *, width: int, height: int):
        file = io.StringIO()
        super().__init__(
            color_system="256",
            file=file,
            width=width,
            height=height,
            force_terminal=False,
            legacy_windows=False,
        )

    @property
    def file(self) -> io.StringIO:
        return cast(io.StringIO, self._file)

    @property
    def is_dumb_terminal(self) -> bool:
        return False


class DriverTest(Driver):
    def start_application_mode(self) -> None:
        size = Size(self.console.size.width, self.console.size.height)
        event = events.Resize(self._target, size, size)
        asyncio.run_coroutine_threadsafe(
            self._target.post_message(event),
            loop=asyncio.get_running_loop(),
        )

    def disable_input(self) -> None:
        pass

    def stop_application_mode(self) -> None:
        pass


def mock_textual_timers(
    *,
    ticks_granularity_fps: int = 60,
) -> ContextManager[MockedTimeMoveClockForward]:
    single_tick_duration = 1.0 / ticks_granularity_fps

    pending_sleep_events: list[tuple[float, asyncio.Event]] = []

    @contextlib.contextmanager
    def mock_textual_timers_context_manager():
        # N.B. `start_time` is not used, but it is useful to have when we set breakpoints there :-)
        start_time = current_time = monotonic()

        # Our replacement for "textual._timer.Timer._sleep":
        async def sleep_mock(duration: float) -> None:
            event = asyncio.Event()
            target_event_monotonic_time = current_time + duration
            pending_sleep_events.append((target_event_monotonic_time, event))
            # Ok, let's wait for this Event
            # (which can only be "unlocked" by calls to `move_clock_forward()`)
            await event.wait()

        # Our replacement for "textual._timer.Timer.get_time" and "textual.message.Message._get_time":
        def get_time_mock() -> float:
            nonlocal current_time

            # let's make the time advance slightly between 2 consecutive calls of this function,
            # within the same order of magnitude than 2 consecutive calls to ` timer.monotonic()`:
            current_time += 1.1e-06

            return current_time

        async def move_clock_forward(*, seconds: float) -> tuple[float, int]:
            nonlocal current_time, start_time

            ticks_count = ceil(seconds * ticks_granularity_fps)
            activated_timers_count_total = 0
            for tick_counter in range(ticks_count):
                current_time += single_tick_duration
                activated_timers_count = check_sleep_timers_to_activate()
                activated_timers_count_total += activated_timers_count

            # Let's give an opportunity to asyncio-related stuff to happen,
            # now that we likely unlocked some occurrences of `await sleep(duration)`:
            await asyncio.sleep(0.001)

            return current_time, activated_timers_count_total

        def check_sleep_timers_to_activate() -> int:
            nonlocal pending_sleep_events

            activated_timers_count = 0
            for i, (target_event_monotonic_time, event) in enumerate(
                pending_sleep_events
            ):
                if current_time < target_event_monotonic_time:
                    continue # not time for you yet, dear awaiter...
                # Right, let's release this waiting event!
                event.set()
                activated_timers_count += 1
                # ...and remove it from our pending sleep events list:
                del pending_sleep_events[i]

            return activated_timers_count

        with mock.patch("textual._timer._TIMERS_CAN_SKIP", new=False), mock.patch(
            "textual._timer.Timer._sleep", side_effect=sleep_mock
        ), mock.patch(
            "textual._timer.Timer.get_time", side_effect=get_time_mock
        ), mock.patch(
            "textual.message.Message._get_time", side_effect=get_time_mock
        ):
            yield move_clock_forward

    return mock_textual_timers_context_manager()
