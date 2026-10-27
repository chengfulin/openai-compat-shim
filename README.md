# openai-compat-shim

一層 OpenAI 相容的反向代理,擋在上游 OpenAI 相容服務(例如自建的 vLLM)前面,
轉發時把**客戶環境的 model name 與 API key 換成內部環境的值**。

> **實驗性質(Experimental)**:開發測試用的小工具,未經正式環境驗證,
> 介面與行為可能隨時調整,請勿直接用於正式環境。

## 為什麼需要它

開發時遇過這種狀況:團隊內部環境自建的 vLLM,model name 與 API key 跟客戶環境不同
(客戶環境由客戶自行管理,後來才對齊)。系統設定寫的是客戶環境的值,但開發、測試
要打的是內部環境。

有了這個 shim,系統可以**維持使用 OpenAI API library、設定照客戶環境填**,
程式與設定都不必為了測試而改動。

**適用範圍**:服務本身不方便動態切換 model name 與 API key,且**上游只有一個**
OpenAI 相容服務。若系統能直接切換這些設定,就不需要這一層;若要接多個上游、
做 fallback、virtual key 管理、用量統計或限流,或要當成長期使用的 gateway,
請改用 [LiteLLM Proxy](https://docs.litellm.ai/docs/simple_proxy) 等成熟方案,
不建議在這個 repo 上擴充。

## 運作方式

| | 客戶端送出(客戶環境的值) | 上游收到(內部環境的值) |
| --- | --- | --- |
| model | `gemma4-26b` | `Gemma-4-26B-A4B` |
| API key | `sk-customer-key` | `sk-internal-key` |

回應中的 `model` 欄位會改回客戶端原本用的名字(串流的每個 chunk 也一樣),
呼叫端從頭到尾只需要認得客戶環境的名稱。

| Method | Path | 說明 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | SDK 的 `create()` 與 `parse()` 共用(後者多帶 `response_format: json_schema`);支援 `stream: true`(SSE) |
| GET | `/v1/models` | 轉發上游清單,並額外列出 alias |
| GET | `/health` | 本地存活檢查,回報上游位址與 alias 表 |

### Model 對應

Alias 比對先試完全相符,再退回不分大小寫;表上沒有的 model 名稱原樣轉發。

### API key 對應

Shim 不自己驗證,依序處理:

1. 客戶端帶的 bearer token(即 `OpenAI(api_key=...)`)若在 `API_KEY_MAP` 中,
   換成對應的上游 key 再轉發。
2. 不在表上的 token 原樣轉發。
3. 客戶端沒帶 `Authorization` 時,才補上 `UPSTREAM_API_KEY`。

### 錯誤處理

上游回的 4xx/5xx(含 `{"error":"Unauthorized"}`)原樣傳回,不改寫。
連不上上游時回 `502`,body 是 OpenAI 格式的 error 物件。

## 設定

環境變數,見 `.env.example`:

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:8001` | 上游位址 |
| `UPSTREAM_API_KEY` | 無 | 後備金鑰 |
| `API_KEY_MAP` | `{}` | JSON 物件,`{"客戶環境 key": "內部環境 key"}` |
| `MODEL_ALIASES` | `{"gemma4-26b":"Gemma-4-26B-A4B"}` | JSON 物件,會**合併**進內建 alias 表 |
| `CONNECT_TIMEOUT` | `10` | 秒 |
| `READ_TIMEOUT` | `600` | 秒;串流逐 token 之間的等待也吃這個值 |

## 啟動

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"          # 只跑 server 的話 `uv pip install -e .` 即可
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 使用

```python
from openai import OpenAI
from pydantic import BaseModel

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<客戶環境的 key>")

# create()
client.chat.completions.create(
    model="gemma4-26b",
    messages=[{"role": "user", "content": "hi"}],
)

# parse()
class Weather(BaseModel):
    city: str
    temperature_c: float

client.chat.completions.parse(
    model="gemma4-26b",
    messages=[{"role": "user", "content": "台北今天幾度?"}],
    response_format=Weather,
)
```

完整範例(含 streaming):`.venv/bin/python examples/client_demo.py`

## 測試

```bash
.venv/bin/python -m pytest
```

16 個測試,全部離線跑:`tests/conftest.py` 起一個假上游(bearer 驗證、model 清單、
串流/非串流 chat、認得 `json_schema`),用 `httpx.ASGITransport` 直接掛給 shim,
不碰網路。其中三個測試拿真正的 `openai` SDK(`create()`、streaming、`parse()`)
打進 shim,確認 SDK 端的 `message.parsed` 真的能組出 pydantic 物件。

## 已知限制

- Shim 不改寫 `response_format`,直接轉發給上游。若上游不支援 `json_schema`
  結構化輸出,`parse()` 會拿到非 JSON 的 content 而在 SDK 端解析失敗,shim 這層無法補。
  (尚未實測:目前沒有上游的 API key,只驗證到 shim 正確連上並轉發、上游回 401。)
- 只代理上述三個路徑,`/v1/embeddings`、`/v1/completions` 等未涵蓋。

## License

[MIT](LICENSE)
