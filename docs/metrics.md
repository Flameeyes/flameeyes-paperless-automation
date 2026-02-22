&lt;!-- SPDX-FileCopyrightText: 2026 Diego Elio Pettenò -->
&lt;!-- SPDX-License-Identifier: MIT -->

# Metrics

The webapp exposes Prometheus metrics at `GET /metrics` in the standard
Prometheus text exposition format (content-type
`text/plain; version=0.0.4`). No authentication is required; secure
access at the network level.

Default listen address: `http://0.0.0.0:8080/metrics`

---

## Document identification

### `documents_identified_total` — Counter

Incremented once per document identification attempt, regardless of
outcome.

| Label | Values | Meaning |
|-------|--------|---------|
| `method` | `pdfrenamer` | Rule-based identification via pdfrename library |
| | `vision` | VLM-based identification via Ollama |
| `status` | `success` | Document identified and Paperless updated |
| | `not_found` | Ran to completion but found nothing to rename |
| | `error` | Unexpected failure (processing exception, VLM timeout, connection error, …) |

**Useful queries**

```promql
# Overall success rate (all methods combined)
sum(rate(documents_identified_total{status="success"}[1h]))
/
sum(rate(documents_identified_total[1h]))

# Per-method success rate
sum by (method) (rate(documents_identified_total{status="success"}[1h]))
/
sum by (method) (rate(documents_identified_total[1h]))

# Error rate to alert on
sum(rate(documents_identified_total{status="error"}[15m]))
```

---

### `document_identification_seconds` — Histogram

Wall-clock time for one full document identification, from PDF retrieval
through any Paperless API writes. Labelled by `method`.

Buckets (seconds): `1, 5, 10, 30, 60, 120, 300, 600, 900`

| Label | Values |
|-------|--------|
| `method` | `pdfrenamer`, `vision` |

**Useful queries**

```promql
# p95 identification time per method
histogram_quantile(0.95,
  sum by (method, le) (rate(document_identification_seconds_bucket[1h]))
)

# Average time per method
sum by (method) (rate(document_identification_seconds_sum[1h]))
/
sum by (method) (rate(document_identification_seconds_count[1h]))
```

---

### `vision_fallbacks_total` — Counter

Incremented each time pdfrenamer found no match for a document and the
vision fallback path was triggered. Only counts when the webapp was
started with `--vision-fallback`.

**Useful queries**

```promql
# Fallback rate relative to total identification requests
rate(vision_fallbacks_total[1h])
/
sum(rate(documents_identified_total{method="pdfrenamer"}[1h]))
```

---

## VLM / Ollama

These metrics cover the Ollama API calls made by the vision path.
All are labelled by `model` (the Ollama model name, e.g. `gemma3:12b`).

### `vlm_requests_total` — Counter

Incremented once per Ollama `chat()` call sequence (i.e. once per
document, after all retry attempts are exhausted).

| Label | Values | Meaning |
|-------|--------|---------|
| `model` | e.g. `gemma3:12b` | Ollama model used |
| `status` | `success` | Response received and parsed successfully |
| | `parse_error` | Response received but could not be parsed as valid JSON |
| | `timeout` | All retry attempts exhausted due to read timeout |
| | `connection_error` | All retry attempts exhausted due to transport/connection failure |
| | `http_error` | Ollama returned an HTTP error status |
| | `model_unavailable` | Model not found on the Ollama server at request time |
| | `no_pages` | PDF rendered to zero pages; Ollama was never called |

**Useful queries**

```promql
# VLM error rate by model
sum by (model, status) (rate(vlm_requests_total{status!="success"}[1h]))

# Alert: sustained timeout rate
rate(vlm_requests_total{status="timeout"}[15m]) > 0
```

---

### `vlm_request_seconds` — Histogram

Wall-clock time of the Ollama `chat()` call(s) for a single document,
including all retry attempts. Labelled by `model`.

Buckets (seconds): `1, 5, 10, 30, 60, 120, 300, 600, 900`

**Useful queries**

```promql
# p95 VLM response time per model
histogram_quantile(0.95,
  sum by (model, le) (rate(vlm_request_seconds_bucket[1h]))
)
```

---

### `vlm_context_tokens` — Histogram

The `num_ctx` value (context window size in tokens) computed and sent to
Ollama for each request. This is calculated as:

```
num_ctx = max(8192, 4096 + total_images × 3072)
```

where `total_images` = (pages per document) + (pages across all
few-shot examples). Tracking this shows how the context window grows as
training examples are added.

Buckets (tokens): `8192, 10240, 12288, 16384, 20480, 24576, 32768, 49152, 65536`

**Useful queries**

```promql
# Average context size over time — watch this grow as examples are added
sum by (model) (rate(vlm_context_tokens_sum[1h]))
/
sum by (model) (rate(vlm_context_tokens_count[1h]))
```

---

### `vlm_few_shot_examples` — Gauge

Number of few-shot training examples loaded from disk for the most
recent VLM request. Updated on each call. Decreases if examples are
removed from the examples directory.

**Useful queries**

```promql
# Current example count per model
vlm_few_shot_examples
```

---

## Suggested alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| High error rate | `rate(documents_identified_total{status="error"}[15m]) > 0.1` | warning |
| VLM unavailable | `rate(vlm_requests_total{status=~"model_unavailable|connection_error"}[5m]) > 0` | critical |
| VLM timeouts | `rate(vlm_requests_total{status="timeout"}[15m]) > 0` | warning |
| No documents processed | `sum(rate(documents_identified_total[30m])) == 0` (during expected activity hours) | warning |
| Context window near limit | `vlm_few_shot_examples > 5` (tune to your `max_few_shot_examples` setting) | info |

---

## Alloy scrape config

```alloy
prometheus.scrape "paperless_automation" {
  targets = [{
    __address__ = "localhost:8080",
  }]
  forward_to = [prometheus.remote_write.default.receiver]
}
```

Adjust `__address__` to wherever the webapp is reachable from your
Alloy instance.
