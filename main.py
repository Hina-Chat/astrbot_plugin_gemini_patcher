# V6.0.0 (Refactored for Low Coupling)
# This version uses a wrapper/decorator pattern instead of full method replacement.

# [!] MAINTAINABILITY NOTE [!]
# This plugin operates by monkey-patching core methods of AstrBot's `ProviderGoogleGenAI`.
# It "wraps" the original methods to inject logic before and after their execution.
#
# PROS: This approach is resilient to most upstream changes in `gemini_source.py`,
# as it does not depend on the internal implementation of the original methods.
#
# CONS: It still depends on the *signatures* of the patched methods and the *structure*
# of the objects they accept and return. A major refactoring in AstrBot could still
# require this plugin to be updated, but the risk is significantly lower.
# [!] END OF NOTE [!]

import logging

from astrbot.api.star import Star, register, Context
from astrbot.core.provider.sources.gemini_source import ProviderGoogleGenAI
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.message.message_event_result import MessageChain

logger = logging.getLogger(__name__)

# Attempt to import google types, handle potential errors
try:
    from google.genai import types

    # A simple check to see if the import was successful and has the expected members.
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

# Store original methods
_original_prepare_query_config = None
_original_process_content_parts = None

# --- Patched Methods (Low-Coupling Wrapper Pattern) ---


async def _patched_prepare_query_config(
    self: ProviderGoogleGenAI, *args, **kwargs
) -> types.GenerateContentConfig:
    """
    Patched version of _prepare_query_config using a wrapper approach.
    It calls the original method and then injects the `thinking_config`.
    This reduces coupling by not relying on the original method's implementation.
    """
    # Call the original method to get the base config
    original_config = await _original_prepare_query_config(self, *args, **kwargs)

    # Inject our thinking_config
    if self.provider_config.get("gm_include_thoughts", True):
        logger.debug("GeminiPatcher: Injecting thinking_config.")
        original_config.thinking_config = types.ThinkingConfig(
            include_thoughts=True,
            thinking_budget=self.provider_config.get("gm_thinking_budget", 2048),
        )

    return original_config


def _patched_process_content_parts(
    result: types.GenerateContentResponse, llm_response: LLMResponse
) -> MessageChain:
    """
    Patched version of _process_content_parts using a wrapper approach.
    It intercepts the response, extracts thought parts, attaches them to the
    LLMResponse object, and then passes the cleaned response to the original method.
    This decouples the plugin from the core logic of how parts are processed.
    """
    thinking_text = []
    final_parts = []

    # Safely access and filter parts, separating thoughts from final content
    try:
        original_parts = result.candidates[0].content.parts
        for part in original_parts:
            if hasattr(part, "thought") and part.thought and part.text:
                logger.debug(
                    f"GeminiPatcher: Captured a thought part: '{part.text[:100]}...'"
                )
                thinking_text.append(part.text)
            else:
                final_parts.append(part)

        # Modify the result object in-place to only contain non-thought parts
        result.candidates[0].content.parts[:] = final_parts

    except (IndexError, AttributeError):
        # If response is malformed, do nothing and let the original method handle it
        pass

    # Attach the captured reasoning content to the response object
    if thinking_text:
        reasoning_content = "\n\n".join(thinking_text)
        logger.debug("GeminiPatcher: Attaching reasoning_content to LLMResponse.")
        setattr(llm_response, "reasoning_content", reasoning_content)

    # Call the original method with the cleaned result, letting it handle all real processing
    return _original_process_content_parts.__func__(result, llm_response)


@register(
    name="astrbot_plugin_gemini_patcher",
    author="Magstic, Gemini 2.5 Pro",
    version="1.0",
    desc="為 Astrbot 的 Gemini 提供 COT 捕獲功能。",
)
class GeminiPatcher(Star):
    """
    A non-intrusive plugin to monkey-patch the ProviderGoogleGenAI.

    It ensures the Gemini thinking process is requested and correctly parsed,
    allowing the hina_think plugin to capture it.

    This plugin uses a low-coupling "wrapper" approach, making it more resilient
    to future updates in AstrBot's core code. See the note at the top of this file.
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        global _original_prepare_query_config, _original_process_content_parts

        if ProviderGoogleGenAI is None:
            logger.error(
                "GeminiPatcherPlugin: Cannot apply patches because ProviderGoogleGenAI is not available."
            )
            return

        logger.info("GeminiPatcherPlugin: Initializing and applying patches...")

        _original_prepare_query_config = ProviderGoogleGenAI._prepare_query_config
        _original_process_content_parts = ProviderGoogleGenAI.__dict__[
            "_process_content_parts"
        ]

        ProviderGoogleGenAI._prepare_query_config = _patched_prepare_query_config
        ProviderGoogleGenAI._process_content_parts = staticmethod(
            _patched_process_content_parts
        )

        logger.info(
            "GeminiPatcherPlugin: Patches for _prepare_query_config and _process_content_parts applied successfully."
        )

    async def terminate(self):
        global _original_prepare_query_config, _original_process_content_parts

        if ProviderGoogleGenAI is None or _original_prepare_query_config is None:
            logger.info("GeminiPatcherPlugin: No patches to remove.")
            return

        logger.info("GeminiPatcherPlugin: Terminating and removing patches...")

        ProviderGoogleGenAI._prepare_query_config = _original_prepare_query_config
        ProviderGoogleGenAI._process_content_parts = _original_process_content_parts

        logger.info("GeminiPatcherPlugin: Patches removed successfully.")
