# Gemini COT 捕獲插件 (astrbot_plugin_gemini_patcher)

Powered by Gemini 2.5 Pro, All.

---

## 插件目標

本插件旨在為 AstrBot 的 Gemini (Google GenAI) 模型提供「思考過程」內容的動態提取能力。它透過一種非侵入式的方式，在不修改 AstrBot 核心源碼的前提下，實現了以下目標：

-   **功能擴展**：讓 Gemini Provider 能夠請求並解析出模型在生成回答時的內部思考鏈總結 (Chain-of-Thought)。
-   **插件協同**：允許下游插件（[Hina_Think](https://github.com/Hina-Chat/astrbot_plugin_hina_think)）無縫使用 Gemini 的推理內容，以實現更複雜的應用場景。
-   **穩定可靠**：解決了在猴子補丁生命週期中因 Python 方法類型機制導致的 `TypeError`，確保插件在啟用、禁用和重載時的絕對穩定性。

## 技術實現：健壯的猴子補丁

本插件的核心是一種低耦合的「猴子補丁 (Monkey-Patching)」技術，我們稱之為「**包裝模式 (Wrapper Pattern)**」。相較於完全替換核心方法，此模式僅「包裝」原始方法，在其執行前後注入我們的邏輯。這極大降低了與 AstrBot 核心程式碼的耦合度，使其對未來更新更具彈性。

我們針對 `ProviderGoogleGenAI` 類中的兩個核心內部方法進行了包裝：

1.  **`_prepare_query_config` (實例方法)**: 我們包裝此方法，讓原始方法先執行以生成標準的請求設定，然後我們再向其返回的設定物件中注入 `thinking_config`，以開啟思考過程的捕獲。

2.  **`_process_content_parts` (靜態方法)**: 我們包裝此方法，先攔截 Gemini API 的原始回應。從中分離出「思考」部分並將其附加到 `LLMResponse` 物件上；然後，將不含思考部分的「純淨」回應交由原始方法處理。如此一來，我們便無需關心核心框架如何處理最終回覆的複雜邏輯。

### 核心挑戰：靜態方法的生命週期管理

在開發過程中，我們發現對靜態方法 (`@staticmethod`) 進行猴子補丁存在一個非常微妙且致命的陷阱。由於 Python 內部對實例方法和靜態方法的處理機制不同，若處理不當，會在插件禁用並恢復原始方法後，導致災難性的 `TypeError`。這個問題的解決方案是我們這個插件最核心的技術突破，它要求在方法的**備份、應用、調用、恢復**四個環節都做對處理：

-   **備份**：必須通过 `ProviderGoogleGenAI.__dict__['_process_content_parts']` 來備份，以獲取真正的 `staticmethod` 對象，而非其內部的普通函數。
-   **應用**：必須使用 `staticmethod(_patched_function)` 將我們的補丁函數顯式轉換成 `staticmethod` 對象後再賦值給類。
-   **調用**：在補丁函數內部，必須使用 `_original_method.__func__(...)` 來調用備份的原始邏輯。
-   **恢復**：將備份的 `staticmethod` 對象直接賦值回類即可。

## 插件完整運轉流程

為了讓維護者能清晰地理解本插件的運作方式，以下是完整的生命週期與事件流程：

### 1. 插件初始化 (`__init__`)

-   **時機**: AstrBot 載入本插件時觸發。
-   **步驟**:
    1.  **環境檢查**: 嘗試匯入 `google.genai.types`。若函式庫不存在或版本不相容，則記錄錯誤並**禁用所有補丁功能**，插件進入安全閒置狀態。
    2.  **保存原始方法**: 若環境檢查通過，遵循「核心挑戰」中描述的精確方式，將 `ProviderGoogleGenAI` 的原始方法（`_prepare_query_config` 和 `_process_content_parts`）儲存到全域變數中備份。
    3.  **應用補丁**: 將 `ProviderGoogleGenAI` 的這兩個方法，替換為我們精心編寫的、且經過正確類型包裝的 `_patched_...` 函式。
    4.  **完成**: 插件進入待命狀態，猴子補丁已生效。

### 2. LLM 請求階段 (Request Path)

-   **時機**: 使用者發送訊息，觸發 Gemini 模型進行回應。
-   **步驟**:
    1.  AstrBot 核心呼叫 `ProviderGoogleGenAI._prepare_query_config`。
    2.  由於補丁已生效，我們的 `_patched_prepare_query_config` **包裝函式**被執行。
    3.  包裝函式**首先呼叫備份的原始方法**，讓 AstrBot 核心完成所有複雜的請求設定建構工作。
    4.  包裝函式取得原始方法返回的 `config` 物件，並向其**注入 `thinking_config` 屬性**。
    5.  將被修改過的 `config` 物件返回給 AstrBot 核心。核心繼續後續流程，此時發往 Gemini API 的請求已包含「請提供思考過程」的指令。

### 3. LLM 回應階段 (Response Path)

-   **時機**: Gemini API 返回包含思考過程與最終答案的資料後。
-   **步驟**:
    1.  AstrBot 核心呼叫 `ProviderGoogleGenAI._process_content_parts`。
    2.  我們的 `_patched_process_content_parts` **包裝函式**被執行。
    3.  包裝函式遍歷 API 回應中的所有 `part`，將「思考部分」存入一個暫存列表，將「非思考部分」存入另一個列表。
    4.  使用 `setattr`，將暫存的「思考部分」文字動態附加到 `llm_response` 物件的 `reasoning_content` 屬性上，供下游插件使用。
    5.  包裝函式修改 API 回應物件，將其 `parts` 列表替換為「非思考部分」的純淨列表。
    6.  包裝函式**最後呼叫備份的原始方法** (透過 `.__func__`)，並將這個「純淨」的回應物件傳遞給它。原始方法按其固有邏輯處理最終答案，完全無需感知到思考過程已被我們提取。

### 4. 插件終止 (`terminate`)

-   **時機**: 插件被禁用、重載或 AstrBot 關閉時觸發。
-   **步驟**:
    1.  **恢復原始方法**: 從備份的全域變數中取出原始方法（包括 `staticmethod` 對象），將其安全地恢復到 `ProviderGoogleGenAI` 類上，徹底移除我們的補丁。
    2.  **完成**: 系統恢復到如同本插件從未載入過的狀態。

## 如何使用

1.  安裝 `google-generativeai` 函式庫；
2.  安裝本插件，並啟用；
3.  [Hina Think](https://github.com/Hina-Chat/astrbot_plugin_hina_think) 現在可以透過檢查 `LLMResponse` 實例的 `reasoning_content` 屬性來獲取 Gemini 的思考過程。
