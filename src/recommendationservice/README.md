# Recommendation Service

This service provides product recommendations over gRPC. Every call sends the full product catalog to OpenRouter's `openai/gpt-oss-120b:free` model (via the official `openai` Python SDK with a custom base URL) so the AI service can return context-aware product IDs. An OpenRouter API key is now required; the service will exit during startup if the key is missing. To allow use of `openai/gpt-oss-120b:free`, OpenRouter [priacy settings](https://openrouter.ai/settings/privacy) must be updated to allow `Enable free endpoints that may publish prompts`

## Local development

```
cd src/recommendationservice
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Required environment

| Variable | Description |
| --- | --- |
| `PRODUCT_CATALOG_SERVICE_ADDR` | Host:port for the Go product catalog gRPC server (for example `localhost:3550`). |
| `OPENROUTER_API_KEY` | API key for https://openrouter.ai. The service will not start without this value. |
| `OPENROUTER_SITE_URL` | Optional Referer header recommended by OpenRouter for rate limiting context. |
| `OPENROUTER_APP_NAME` | Optional label for the `X-Title` header (defaults to `recommendationservice`). |
| `OPENROUTER_TIMEOUT_SECONDS` | Custom request timeout for OpenRouter calls. |

Run the product catalog service separately (`go run .` from `src/productcatalogservice`), then start the recommendation service:

```
export PRODUCT_CATALOG_SERVICE_ADDR=localhost:3550
export OPENROUTER_API_KEY=your-key
python recommendation_server.py
```

Use `client.py` (or grpcurl) to exercise the `ListRecommendations` RPC. The request payload sent to OpenRouter includes the full catalog, the active user, and the items already in the cart, and expects a JSON response containing product IDs to return to callers.
