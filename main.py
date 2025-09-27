# 此版本採用包裝器/裝飾器模式，而非完全取代方法。
#
# 此插件透過對 AstrBot 的 `ProviderGoogleGenAI` 核心方法進行猴子補丁（monkey-patching）來運作。
# 它「包裝」了原始方法，以便在方法執行前後注入邏輯。確保 Gemini 的思考歷程被請求並正確解析，讓下游插件得以擷取。
#
# 優點：這種方法對於 `gemini_source.py` 的多數上游變更具有韌性，因為它不依賴於原始方法的內部實作。
#
# 缺點：它仍然依賴於被修補方法的簽章（signatures）以及其接受和回傳物件的結構，AstrBot 的重大重構可能仍需要更新此插件，但風險已顯著降低。

import logging

from astrbot.api.star import Star, Context
from astrbot.core.provider.sources.gemini_source import ProviderGoogleGenAI
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.message.message_event_result import MessageChain

logger = logging.getLogger(__name__)

# 嘗試匯入 Google 類型，處理潛在錯誤
try:
    from google.genai import types

    # 一個簡單的檢查，看看匯入是否成功並且具有預期的成員。
    if not hasattr(types, "GenerateContentConfig"):
        raise ImportError(
            "The installed 'google-genai' library is of an incompatible version."
        )
except ImportError as e:
    logger.error(
        f"Failed to import 'google.genai.types' or it's incompatible. GeminiPatcher will be disabled. Error: {e}"
    )
    types = None
    ProviderGoogleGenAI = None  # 若 SDK 無法使用，則不進行修補

# 儲存原始方法
_original_prepare_query_config = None
_original_process_content_parts = None

# --- 修補方法 (低耦合包裝模式) ---


async def _patched_prepare_query_config(
    self: ProviderGoogleGenAI, *args, **kwargs
) -> types.GenerateContentConfig:
    """
    採用包裝器方法的 `_prepare_query_config` 修補版本。
    它會呼叫原始方法，然後注入 `thinking_config`。
    此作法不依賴原始方法的實作，因而降低了耦合度。
    """
    # 呼叫原始方法以取得基礎設定
    original_config = await _original_prepare_query_config(self, *args, **kwargs)

    # 注入我們的 thinking_config
    if self.provider_config.get("gm_include_thoughts", True):
        logger.debug("GeminiPatcher: Injecting thinking_config.")
        try:
            original_config.thinking_config = types.ThinkingConfig(
                include_thoughts=True,
                thinking_budget=self.provider_config.get("gm_thinking_budget", 2048),
            )
        except TypeError:
            # Fallback for SDKs that don't support include_thoughts param
            original_config.thinking_config = types.ThinkingConfig(
                thinking_budget=self.provider_config.get("gm_thinking_budget", 2048),
            )

    return original_config


def _patched_process_content_parts(
    candidate: types.Candidate, llm_response: LLMResponse
) -> MessageChain:
    """
    採用包裝器方法的 `_process_content_parts` 修補版本。
    它會攔截回應 Candidate，提取思考歷程部分 (thought parts)，將其附加到 LLMResponse 物件，然後將清理後的回應傳遞給原始方法。
    這將插件與處理內容部分 (parts) 的核心邏輯解耦。
    """
    thinking_text = []
    final_parts = []

    # 安全地存取並過濾內容，將思考歷程與最終內容分離
    try:
        if not candidate or not getattr(candidate, "content", None):
            raise AttributeError("Candidate has no content.")
        original_parts = candidate.content.parts or []
        for part in original_parts:
            # 盡可能兼容不同 SDK 版本的思考標記
            is_thought = bool(getattr(part, "thought", False))
            if is_thought and getattr(part, "text", None):
                logger.debug(
                    f"GeminiPatcher: Captured a thought part: '{part.text[:100]}...'"
                )
                thinking_text.append(part.text)
            else:
                final_parts.append(part)

        # 就地修改結果物件，使其僅包含非思考歷程的部分
        candidate.content.parts[:] = final_parts

    except (IndexError, AttributeError):
        # 若回應格式有誤，則不做任何處理，並讓原始方法應對
        pass

    # 將擷取到的思考歷程內容附加到回應物件
    if thinking_text:
        reasoning_content = "\n\n".join(thinking_text)
        logger.debug("GeminiPatcher: Attaching reasoning_content to LLMResponse.")
        setattr(llm_response, "reasoning_content", reasoning_content)

    # 呼叫原始方法並傳入清理後的結果，讓它處理所有實際的程序
    return _original_process_content_parts.__func__(candidate, llm_response)

class GeminiPatcher(Star):

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
