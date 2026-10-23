"""The voice server: one pipecat bot per voice client on the stock runner, with the routes of
the mod, the tray and the settings page."""

from __future__ import annotations

import sys

from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.run import app, main
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

from . import desktop, routes, services, settings, web
from .bridge import DEVELOPER, TOOLS, Bridge
from .processors import Microphone, MuteGate, Preamble

IDLE_SECONDS = 3600.0  # a Claude Code turn can run for minutes in silence
POLL_SECONDS = 25.0  # under the 30 s limit of a mod's HTTP call

BRIDGE = Bridge(interval=30.0)
MICROPHONE = Microphone()


async def bot(runner_args: RunnerArguments) -> None:
    """Runs one voice session with the settings saved now. The runner calls it per client."""
    current = settings.load()
    BRIDGE.interval = current.narration_seconds
    params = {"webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True)}
    transport = await create_transport(runner_args, params)
    llm = services.llm(current.llm, prompt=current.prompt)
    BRIDGE.register(llm)

    context = LLMContext(tools=ToolsSchema(standard_tools=list(TOOLS)))
    window = await services.window(current.llm)
    user, assistant = LLMContextAggregatorPair(
        context,
        user_params=services.turn(),
        assistant_params=services.context(current.llm, window=window),
    )
    pipeline = Pipeline(
        [
            transport.input(),
            MuteGate(MICROPHONE),
            services.stt(current.stt),
            user,
            llm,
            Preamble(),
            services.tts(current.tts, speed=current.speed),
            transport.output(),
            assistant,
        ]
    )
    worker = PipelineWorker(
        pipeline, idle_timeout_secs=IDLE_SECONDS, params=PipelineParams(enable_metrics=True)
    )
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    @worker.rtvi.event_handler("on_client_ready")
    async def _on_client_ready(rtvi: object) -> None:
        greeting = {"role": DEVELOPER, "content": f"{current.greeting}\n{BRIDGE.linked_line()}"}
        await worker.queue_frame(LLMMessagesAppendFrame(messages=[greeting], run_llm=True))

    @transport.event_handler("on_client_disconnected")
    async def _on_client_disconnected(transport: object, client: object) -> None:
        await runner.cancel()

    BRIDGE.attach(worker)
    try:
        await runner.run()
    finally:
        BRIDGE.detach(worker)


def serve() -> None:
    """Serves on this computer only. Missing keys do not stop the server: the settings page
    names them."""
    port, token = settings.port(), settings.token()
    routes.mount(
        app, bridge=BRIDGE, microphone=MICROPHONE, token=token, port=port, poll=POLL_SECONDS
    )
    web.mount(app, token=token)
    desktop.lifespan(app, port=port, active=BRIDGE.active)
    for problem in settings.problems():
        logger.warning("voice-relay: {}", problem)
    sys.modules["__main__"].bot = bot  # type: ignore[attr-defined]  # the runner looks there
    sys.argv = [sys.argv[0], "-t", "webrtc", "--host", settings.HOST, "--port", str(port)]
    main()


if __name__ == "__main__":
    serve()
