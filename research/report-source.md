# Research source: Google AI Mode follow-up failures

Date: 2026-09-07

## Research question

Why does the deployed browser worker reliably obtain a first Google AI Mode answer but often receive Google's generic “content could not be generated” block on a follow-up, while a manual browser flow can work?

## Bottom line

Bot/abuse risk gating is a plausible leading hypothesis, but the visible error is not proof. There is no public Google documentation mapping the localized generic error element (`[data-aeie="true"]`) to bot detection. The most important next step is to record the network result for one follow-up attempt, with authentication material redacted, before doing more behavioral or account experiments.

Confidence that some form of risk/abuse gating contributes: medium-high (~70%). Confidence that it is the only cause: low.

## Primary-source findings

- Google explicitly designs AI Mode for conversational follow-up questions. Follow-ups are therefore normal product use, not an unsupported UI trick. Google also says AI Mode uses query fan-out and issues multiple simultaneous searches. Source: https://blog.google/products-and-platforms/products/search/google-search-ai-mode-update/
- Google says Search may show “unusual traffic” when a network, including a VPN, appears to send automated traffic. It explicitly lists robots, computer programs, automated services, and search scrapers as automated traffic. Source: https://support.google.com/websearch/answer/86640?hl=en
- Google Search's policy defines machine-generated traffic as automated queries and other automated access without express permission, and says it violates Google policies and Terms. Source: https://developers.google.com/search/docs/essentials/spam-policies
- Google's Terms prohibit bypassing protective measures and some automated access. Source: https://policies.google.com/terms?hl=en
- Google's commercial reCAPTCHA/Fraud Defense documentation says risk can be assessed per action using user behavior, device information, IP addresses, and historical interaction patterns. This demonstrates signal classes Google uses in a defense product, but does not prove Google Search AI Mode uses the identical implementation. Source: https://cloud.google.com/security/products/recaptcha
- Playwright documents that `keyboard.type()` emits the normal key event sequence for US-keyboard characters, while characters not on a US keyboard emit only an `input` event. Source: https://playwright.dev/docs/api/class-keyboard
- Camoufox's own 2026 documentation says its performance has declined because of its Firefox base and discovered fingerprint inconsistencies. It hides Playwright's page-scope traces but acknowledges behavior/timing analysis and internally inconsistent fingerprints remain detectable. Source: https://camoufox.com/stealth/
- A July 2026 controlled-study preprint reports that missing raw pointer/wheel event streams remained a strong Playwright automation signal even under trajectory replay. It is not Google-specific and should be treated as supporting, not direct, evidence. Source: https://arxiv.org/abs/2607.26935
- Google Account Help says new-device security events are evaluated with device, time, and location context. This supports treating a newly transferred account/profile as a trust variable, but does not document AI Mode gating. Source: https://support.google.com/accounts/answer/2590353?hl=en

## Local evidence

Observed production behavior:

- Multiple harmless first questions produced AI Mode answers.
- Multiple harmless follow-ups produced Google's own localized generic error element rather than `/sorry/` or a locator timeout.
- The correct visible follow-up textarea was found, received the exact input, and submitted successfully.
- The failure appeared in Hindi, French, and Spanish across launches.
- A direct `udm=50` route fixed cases where the AI Mode tab was absent, proving that missing tab markup was a separate UI/rollout issue.
- A manual local browser session routed through the production exit IP reportedly completed a follow-up. This is a useful observation but was not captured as a controlled A/B run.

Code/configuration facts:

- One persistent profile/device/IP received different browser interface languages between launches.
- The inspected production `CAMOU_CONFIG` combined a French region and Paris timezone with Spanish language on one launch. Earlier screenshots showed French and Hindi.
- `AIFARM_GEOIP=true` lets launch-time geo data regenerate volatile locale fields, while the durable device fingerprint excludes them.
- `AIFARM_SEED_COOKIES=true` injects English-flavored `SOCS` plus `CONSENT` even when the runtime UI language differs.
- Follow-ups use Playwright typing and scrolling. Cyrillic input has a documented event-shape difference in Playwright.
- The new wrong-keyboard-layout transformation changes a valid Russian question into semantically meaningless Latin text. Its single production trial also failed, so it neither fixed nor cleanly tested bot detection.

## Hypotheses, ranked

1. **Composite risk/abuse gating — 0.70.** VPS/datacenter IP reputation, new-account/new-device trust, changing locale identity, and automation behavior can combine into an action-level risk decision. First-answer success does not rule this out because risk can be evaluated on each action.
2. **AI Mode backend/experiment instability — 0.35.** The generic error has no documented reason code, and generative Search is experimental. A follow-up can fail even when the request is allowed.
3. **Account entitlement or trust state — 0.30.** A newly acquired account and new login environment may have reduced trust or inconsistent feature state. Public docs do not establish this for AI Mode.
4. **Content/safety rejection — 0.10 for the original Russian tests.** The failed factual questions were benign. The wrong-layout trial is not evidence because the resulting gibberish may itself be rejected.
5. **DOM selector or Enter-submit bug — 0.05.** Screenshots/dumps show the question bubble and Google's own generated error block, so the submit reached the application.

Probabilities are directional and not mutually exclusive.

## Why follow-up can fail while the first answer succeeds

This is an inference, not a documented Google mechanism. A follow-up is a second protected action inside a stateful, telemetry-rich application. Google says AI Mode performs query fan-out, so the second generation may carry greater resource/risk cost than navigation into the initial page. Risk systems can score each action rather than blocking the entire session. Thus “first works, second fails” is consistent with gating, but does not identify it conclusively.

## Recommended experiment order

1. Add redacted diagnostics for the AI Mode generation request/response on exactly one follow-up: URL host/path, method, status, timing, response content type, a bounded/redacted response excerpt, failed requests, and console errors. Never persist cookies, authorization headers, request bodies, account identifiers, or full HAR files.
2. Classify the outcome:
   - HTTP 403/429, challenge, or explicit abuse/risk marker strongly supports gating.
   - HTTP 200 with a generation status such as safety/capacity/product error points to the AI backend or policy layer.
   - No request after Enter points back to client-state/input handling.
3. Revert the wrong-layout conversion before testing; it corrupts user intent and confounds the experiment.
4. Make locale identity stable for a profile. Persist the initially selected locale/timezone/region as part of that profile, and do not mutate an already warmed identity repeatedly. Do not blindly force random locale values each launch.
5. Stop injecting default English consent cookies into a profile that already has real cookies, or at minimum make their language coherent with the pinned locale.
6. Run only a tiny controlled A/B after cooldown: same account, same IP, same harmless question pair, one manual attempt and one automated attempt, never concurrently on the same profile. Record the exact network outcome in both.
7. Avoid high-volume repeated probes. Besides risking the account/IP, automated Search queries are explicitly disallowed by Google unless permission applies.

## Operational recommendation

For a production integration, use a supported Google API/product rather than browser automation against Search. If browser-driven access remains a temporary diagnostic, keep it low-volume, observable, and do not attempt to bypass challenges or protective measures.

## Evidence limits

- Google does not publish the internal AI Mode follow-up risk model.
- reCAPTCHA product documentation is evidence about available Google defense signals, not proof of Search's exact implementation.
- The academic paper is a controlled preprint and is not Google-specific.
- The manual-success comparison was not conducted as a recorded, controlled experiment.
- The UI error contains no public machine-readable explanation in the evidence inspected so far.
