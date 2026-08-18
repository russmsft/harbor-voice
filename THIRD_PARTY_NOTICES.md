# Third-party notices

Harbor Voice depends on independently licensed packages and models. Their licences apply to those components and are not replaced by Harbor Voice's MIT licence.

Major runtime components include:

- OpenAI Codex Python SDK and its bundled runtime (Apache-2.0 package metadata).
- PySide6 / Qt for Python (LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only in the
  installed package metadata; separate commercial terms may also be available).
- faster-whisper and CTranslate2 (MIT).
- Whisper model files, subject to the licence distributed with the selected model.
- NumPy (a combination of BSD-3-Clause, 0BSD, MIT, Zlib, and CC0-1.0 terms in
  the installed package metadata).
- pynput (LGPLv3).
- sounddevice (MIT).
- pyttsx3 (MPL-2.0).
- Pydantic (MIT).
- platformdirs (MIT).
- qasync (BSD-2-Clause).

Before public redistribution, regenerate a complete dependency inventory from `uv.lock`, verify the selected Whisper model's licence, and include the required licence texts in the distributable.
