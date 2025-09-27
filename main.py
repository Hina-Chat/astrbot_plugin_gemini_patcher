import logging

from astrbot.api.star import Star, Context
from astrbot.core.provider.sources.gemini_source import ProviderGoogleGenAI
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.event import AstrMessageEvent
import astrbot.core.message.components as Comp

logger = logging.getLogger(__name__)

# Attempt to import google types, handle potential errors
try:
    from google.genai import types

    if not hasattr(types, "GenerateContentConfig"):
        raise ImportError(
            "The installed 'google-genai' library is of an incompatible version."
        )
except ImportError as e:
    logger.error(
        f"Failed to import 'google.genai.types' or it's incompatible. GeminiPatcher will be disabled. Error: {e}"
    )
    types = None
    ProviderGoogleGenAI = None  # Prevent patching if SDK is not available

# --- Store Original Method ---
_original_text_chat = None

# --- Patched Method (Forced Streaming) ---

async def _patched_text_chat(
    self: ProviderGoogleGenAI,
    prompt: str,
    session_id=None,
    image_urls=None,
    func_tool=None,
    contexts=None,
    system_prompt=None,
    tool_calls_result=None,
    model=None,
    **kwargs,
) -> LLMResponse:
    """
    Patched version of text_chat that forces a streaming approach.
    It calls the provider's own `text_chat_stream` method and processes the
    resulting generator to provide hybrid output.
    """
    event: AstrMessageEvent | None = kwargs.get("event")
    final_response_text = []
    full_reasoning_content = []

    # Force the call to the streaming equivalent
    stream_generator = self.text_chat_stream(
        prompt,
        session_id,
        image_urls,
        func_tool,
        contexts,
        system_prompt,
        tool_calls_result,
        model,
        **kwargs,
    )

    async for resp_chunk in stream_generator:
        # The stream from gemini_source already separates thoughts in `reasoning_content`
        if hasattr(resp_chunk, "reasoning_content") and resp_chunk.reasoning_content:
            thought_text = resp_chunk.reasoning_content
            full_reasoning_content.append(thought_text)
            logger.debug(f"GeminiPatcher: Intercepted thought: {thought_text[:100]}...")
            if event:
                try:
                    # Try to send the thought back to the user in real-time
                    await event.reply(MessageChain(chain=[Comp.Plain(f" {thought_text}")]))
                except Exception as e:
                    logger.error(f"GeminiPatcher: Failed to reply with thought: {e}")
        
        # Collect the final answer parts
        if resp_chunk.result_chain:
            final_response_text.append(resp_chunk.result_chain.get_plain_text())

    # Assemble the final, non-streamed response
    final_response = LLMResponse("assistant")
    final_text = "".join(final_response_text).strip()
    if not final_text:
        final_text = "(Empty Response)" # Avoid sending empty messages
    final_response.result_chain = MessageChain(chain=[Comp.Plain(final_text)])
    
    # Attach the complete reasoning log for hina_think
    if full_reasoning_content:
        setattr(final_response, "reasoning_content", "\n\n".join(full_reasoning_content))

    logger.debug("GeminiPatcher: Returning final, assembled, non-streamed answer.")
    return final_response

class GeminiPatcher(Star):
    """
    This plugin forces Gemini to use a streaming-thought process even when
    global streaming is off. It patches `text_chat` to intercept the call,
    force a streaming execution, send back thoughts in real-time, and return
    the final answer as a single response.
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        global _original_text_chat

        if ProviderGoogleGenAI is None:
            logger.error(
                "GeminiPatcherPlugin: Cannot apply patches because ProviderGoogleGenAI is not available."
            )
            return

        logger.info("GeminiPatcherPlugin: Initializing and applying force-stream patch...")

        # Store original method
        _original_text_chat = ProviderGoogleGenAI.text_chat

        # Apply patch
        ProviderGoogleGenAI.text_chat = _patched_text_chat

        logger.info(
            "GeminiPatcherPlugin: Patch for text_chat applied successfully."
        )

    async def terminate(self):
        global _original_text_chat

        if ProviderGoogleGenAI is None or _original_text_chat is None:
            logger.info("GeminiPatcherPlugin: No patches to remove.")
            return

        logger.info("GeminiPatcherPlugin: Terminating and removing patch...")

        # Restore original method
        ProviderGoogleGenAI.text_chat = _original_text_chat

        logger.info("GeminiPatcherPlugin: Patch for text_chat removed successfully.")
