# Worker B interface requests (T6 phase 2)

No frozen file was edited. These are requests and notes for the lead.

1. **Live activation is unreachable by construction (D31 cost).**
   `SynthesisPolicy.pricing.basis` is `Literal["fixture"]`, and the
   activation `cost.pricing_source` can only be `synthesis_policy.pricing`.
   Fixture rates must never price a live run, so `evaluate_activation` always
   ends a fully consistent live config with `cost_policy_missing`. This is the
   intended fail-closed outcome for T6. It does mean no test can produce an
   approved live `ActivationDecision` through `evaluate_activation`, so adapter
   tests build the decision directly. Request: a later versioned synthesis
   policy with an `approved_live` pricing block, when live is approved.

2. **No corpus identity input for `corpus_mismatch`.** The
   `evaluate_activation` signature has no expected corpus ID or generation, so
   `corpus_mismatch` only checks the policy's own consistency (non-empty,
   unique `corpus_ids`, `internal` classification). Request: an optional
   expected-corpus argument, for example `retrieval.provenance.corpus_generation`
   or the index corpus ID, so composition can bind the corpus.

3. **Ambient proxy/TLS env is not part of `ProviderEnvironment`.** Added extra
   public helpers in `application/activation.py` (B-owned):
   `reject_ambient_overrides(environ)`, `activate(...)` (env read, then
   override check for live, then evaluate) and `endpoint_allowed(...)`.
   Composition should call `activate` and not `evaluate_activation` directly.

4. **Adapter request-shape violations raise `SynthesisConfigurationError`**,
   not `SynthesisProviderError`. This covers extra roles, tool definitions,
   `max_output_tokens < 1` and a non-allowlisted payload, all with zero
   requests. A non-positive or non-finite timeout raises
   `SynthesisProviderError(provider_timeout)` with zero requests. Worker A's
   `SynthesizeAnswer` should settle the ledger reservation as a failure for any
   `SynthesisError` raised by `model.synthesize`, not only for
   `SynthesisProviderError`.

5. **The guard requires `messages[0].content == SYNTHESIS_INSTRUCTIONS`** and
   exactly `[system, user]`. A must build `ModelInput(instructions=SYNTHESIS_INSTRUCTIONS)`
   with no `extra_messages` or `tool_definitions` for the live adapter.
