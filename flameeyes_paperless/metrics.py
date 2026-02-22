# SPDX-FileCopyrightText: 2026 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

"""Prometheus metrics for the paperless automation webapp."""

from prometheus_client import Counter, Gauge, Histogram

# Document identification outcomes — both pdfrenamer and vision paths.
# status labels:
#   success     — document identified and updated
#   not_found   — ran OK but found nothing to rename
#   error       — unexpected failure (exception, timeout, connection error, …)
documents_identified_total = Counter(
    "documents_identified_total",
    "Total document identification attempts by method and outcome",
    ["method", "status"],
)

# Wall-clock time for the full identification of one document, per method.
# Includes PDF retrieval, processing, and any Paperless API writes.
document_identification_seconds = Histogram(
    "document_identification_seconds",
    "Wall-clock time for document identification by method",
    ["method"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 900],
)

# Number of times the vision fallback was triggered because pdfrenamer
# found no match.
vision_fallbacks_total = Counter(
    "vision_fallbacks_total",
    "Times vision fallback was triggered after pdfrenamer found no match",
)

# ── VLM / Ollama metrics ────────────────────────────────────────────────────

# Individual Ollama API call outcomes, labelled by model.
# status labels: success | parse_error | timeout | connection_error |
#                http_error | model_unavailable | no_pages
vlm_requests_total = Counter(
    "vlm_requests_total",
    "Ollama VLM API call outcomes by model and status",
    ["model", "status"],
)

# Total wall-clock time of the Ollama chat() call(s) for one document,
# including all retry attempts.
vlm_request_seconds = Histogram(
    "vlm_request_seconds",
    "Wall-clock time for Ollama VLM requests (including retries)",
    ["model"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 900],
)

# Computed num_ctx (context window size in tokens) sent to Ollama.
# Grows as more few-shot examples are loaded.
vlm_context_tokens = Histogram(
    "vlm_context_tokens",
    "Computed context window size (num_ctx) sent to Ollama per request",
    ["model"],
    buckets=[8192, 10240, 12288, 16384, 20480, 24576, 32768, 49152, 65536],
)

# Current number of few-shot examples loaded for the most recent VLM call.
# Gauge rather than Counter because it can decrease if examples are removed.
vlm_few_shot_examples = Gauge(
    "vlm_few_shot_examples",
    "Number of few-shot examples loaded for the last VLM request",
    ["model"],
)
