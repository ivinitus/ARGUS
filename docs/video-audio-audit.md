# Video and audio evidence

Install the `video` extra to enable PyAV decoding. The pipeline validates the
attachment, decodes it locally, and supplies timestamped frames to the configured
image model. The existing evidence judge combines those observations with test
steps and expected results. It never counts an unobserved event as confirmed.

Sampling is bounded to limit payload size and model cost. Brief events between
sampled frames are unverified. The evidence metadata records sampling and
coverage limitations; incomplete or malformed model output is not a passing
result. No video upload bucket is required.

Audio decoding, signal checks, silence detection, bounded chunking, timestamps,
and split/retry logic run locally. Transcription is optional and requires an
audio-capable chat API configured through `auditor.audio_api_url`,
`auditor.video_audio_model_id`, `auditor.audio_api_format`, and a key named by
`auditor.audio_api_key_env`. The supported request format is `openai`.

The model key and audio key may be different. With no audio provider, sound or
voice assertions cannot be certified by visual frames alone; the report retains
that limitation. The configured provider receives selected frames or audio, so
use evidence suitable for that provider.
