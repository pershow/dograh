"""Call-local speaker diagnostics. This package never controls the voice pipeline.

Enable Speaker Tracking (Preview) in workflow General settings, save and publish.
The persisted workflow setting is ``speaker_tracking_enabled`` (default false).
Both telephony and WebRTC attach the same observer; text chats do not.

Local preparation: install api/requirements.txt, then run
``python -m api.services.speaker_tracking.model``. Docker prepackages the weights.
SPEAKER_TRACKING_MODEL_PATH optionally selects the checksum-pinned model file.
Calls never download models. A missing dependency/file emits a disabled event.

Results are saved in workflow-run logs.realtime_feedback_events with type
``rtf-speaker-tracking``. payload.action includes started, ready, candidate,
primary_established, primary_match, primary_changed, other_speaker, uncertain,
skipped, disabled and summary. Window events carry input-audio offsets, similarity
(not a calibrated probability), speech duration and inference timing. Summary
counts include skipped/overloaded samples. Embeddings and PCM are never logged.

References live only in the call analyzer and are discarded on finish. The shared
process cache contains model weights only. Up to eight anonymous voices can be
tracked; unknown voices beyond capacity are logged as uncertain.

Initial primary selection needs three consistent windows and >=3 seconds of
speech. Handoff needs three consecutive windows, >=4.5 seconds of new-speaker
speech and >=4 seconds without the previous primary. These are experimental
defaults; evaluate against labeled calls before using any decisions as gates.
Gray-band windows can establish a new candidate only after three mutually
consistent windows whose similarity exceeds every known voice by >=0.10.
When answer supervision is pending no samples are learned. During bot speech
windows can be classified but cannot update references or establish/switch the
primary. No explicit enrollment, stored identity or cross-call recognition exists.

Single-channel CAM++ embeddings cannot separate overlapping speakers or prove
that someone is addressing the agent. TV speech, echo and persistent background
speakers can still confuse these hypotheses. 8 kHz audio is resampled to 16 kHz;
this does not restore missing bandwidth. Validate these cases in shadow mode.
"""
