# Text Commands & TTS Guide

## TTS Engines

Three engines are available, selected via `TTS_ENGINE` in `gateway_config.txt`:

| Engine | Quality | Requires | Voices |
|--------|---------|----------|--------|
| `kokoro` | High — offline neural (default) | ONNX models in `tools/models/kokoro/` (~340 MB, downloaded by install.sh) | 54 voices across 9 languages |
| `edge` | High — Microsoft Neural (online) | Internet + `edge-tts` pip package | ~300 voices |
| `gtts` | Moderate — Google Translate (online) | Internet + `gtts` pip package | 9 accents (numeric) |

### Installing / upgrading

```bash
# Kokoro (default engine) — pip package only, model files handled by install.sh
pip install kokoro-onnx

# Download Kokoro model files manually if install.sh was not used:
mkdir -p tools/models/kokoro
BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
wget -O tools/models/kokoro/kokoro-v1.0.onnx "$BASE/kokoro-v1.0.onnx"
wget -O tools/models/kokoro/voices-v1.0.bin  "$BASE/voices-v1.0.bin"

# Edge / gTTS (online fallback engines)
pip install edge-tts gtts
```

---

## Configuration (`gateway_config.txt`)

```ini
[tts]
ENABLE_TTS = true
TTS_ENGINE = kokoro             # kokoro | edge | gtts

# Kokoro: string voice ID from the list below
KOKORO_DEFAULT_VOICE = af_heart

# gTTS/Edge: numeric accent (1=US 2=British 3=Australian 4=Indian …)
TTS_DEFAULT_VOICE = 1

TTS_VOLUME = 1.0                # Volume multiplier (1.0 = normal)
TTS_SPEED  = 1.0                # Speed multiplier (1.0 = normal; >1.0 faster; requires ffmpeg)
PTT_TTS_DELAY = 0.5             # PTT pre-key settle time before audio starts
```

---

## Kokoro Voice List

The voice dropdown in `/controls` and `/dashboard` is populated automatically from the active engine. For reference, all 54 Kokoro voices:

### American English (prefix `a`)
| Voice ID | Description |
|----------|-------------|
| `af_heart` | Heart (US F) ★★★★ — warm, natural |
| `af_bella` | Bella (US F) ★★★½ |
| `af_nicole` | Nicole (US F) ★★★ — whisper-y |
| `af_aoede` | Aoede (US F) ★★★ |
| `af_kore` | Kore (US F) ★★★ |
| `af_sarah` | Sarah (US F) ★★★ |
| `af_nova` | Nova (US F) ★★★ |
| `af_sky` | Sky (US F) ★★½ |
| `af_alloy` | Alloy (US F) ★★½ |
| `af_jessica` | Jessica (US F) ★★½ |
| `af_river` | River (US F) ★★½ |
| `am_adam` | Adam (US M) ★★★★ |
| `am_echo` | Echo (US M) ★★★ |
| `am_eric` | Eric (US M) ★★★ |
| `am_fenrir` | Fenrir (US M) ★★★ |
| `am_liam` | Liam (US M) ★★★ |
| `am_michael` | Michael (US M) ★★★ |
| `am_onyx` | Onyx (US M) ★★★ |
| `am_puck` | Puck (US M) ★★★ |
| `am_santa` | Santa (US M) ★ |

### British English (prefix `b`)
| Voice ID | Description |
|----------|-------------|
| `bf_emma` | Emma (GB F) ★★★ |
| `bf_isabella` | Isabella (GB F) ★★★ |
| `bm_george` | George (GB M) ★★★ |
| `bm_lewis` | Lewis (GB M) ★★★ |

### Japanese (prefix `j`)
| Voice ID | Description |
|----------|-------------|
| `jf_alpha` | Alpha (JP F) ★★★ |
| `jf_gongitsune` | Gongitsune (JP F) ★★★ |
| `jf_nezuko` | Nezuko (JP F) ★★★ |
| `jf_tebukuro` | Tebukuro (JP F) ★★★ |
| `jm_kumo` | Kumo (JP M) ★★★ |

### Mandarin Chinese (prefix `z`)
| Voice ID | Description |
|----------|-------------|
| `zf_xiaobei` | Xiaobei (ZH F) ★★★ |
| `zm_yunjian` | Yunjian (ZH M) ★★★ |
| `zm_yunxi` | Yunxi (ZH M) ★★★ |
| `zm_yunxia` | Yunxia (ZH M) ★★★ |
| `zm_yunyang` | Yunyang (ZH M) ★★★ |

### Spanish (prefix `e`)
| Voice ID | Description |
|----------|-------------|
| `ef_dora` | Dora (ES F) ★★★ |
| `em_alex` | Alex (ES M) ★★★ |
| `em_santa` | Santa (ES M) ★★★ |

### French (prefix `f`)
| Voice ID | Description |
|----------|-------------|
| `ff_siwis` | Siwis (FR F) ★★★ |
| `fm_geraint` | Geraint (FR M) ★★★ |

### Hindi (prefix `h`)
| Voice ID | Description |
|----------|-------------|
| `hf_alpha` | Alpha (HI F) ★★★ |
| `hm_omega` | Omega (HI M) ★★★ |

### Italian (prefix `i`)
| Voice ID | Description |
|----------|-------------|
| `if_sara` | Sara (IT F) ★★★ |
| `im_nicola` | Nicola (IT M) ★★★ |

### Portuguese / Brazilian (prefix `p`)
| Voice ID | Description |
|----------|-------------|
| `pf_dora` | Dora (PT F) ★★★ |
| `pm_alex` | Alex (PT M) ★★★ |
| `pm_santa` | Santa (PT M) ★★★ |

---

## Mumble Chat Commands

### !speak \<text\>
Broadcast TTS on radio using the default voice.

```
!speak Net will start in 5 minutes
!speak Emergency traffic — all stations stand by
```

### !speak \<voice\> \<text\>
Override voice for this message only.

**Kokoro engine** — use a voice ID string:
```
!speak af_bella Good morning all stations
!speak bm_george This is the weekly net
!speak am_adam Attention all stations
```

**gTTS/Edge engine** — use a numeric accent:
```
!speak 2 Hello from British voice
!speak 3 G'day from Australian voice
```

Voice IDs: `af_heart af_bella am_adam bm_george bf_emma` — see full list above.

### !cw \<text\>
Send Morse code (CW) on radio.

```
!cw de w1aw
!cw qst qst de n1tpv
!cw 73
```

Config: `CW_WPM` (default 20 wpm), `CW_FREQUENCY` (default 600 Hz), `CW_VOLUME` (default 1.0).

### !play \<0–9\>
Play an announcement file by slot number.

```
!play 0     # Station ID
!play 1     # Announcement slot 1
```

### !files
List loaded announcement files and their slot numbers.

### !stop
Stop playback and clear the queue.

### !mute / !unmute
Mute/unmute Mumble → Radio TX without stopping the gateway.

### !id
Shortcut for `!play 0` (station ID).

### !status
Print current gateway status to Mumble chat.

### !help
Print the command list.

---

## Audio Flow

```
Mumble User → !speak → Gateway → Kokoro ONNX synthesis → WAV (24kHz)
                                                          ↓
                                              Resample to 48kHz (resampy)
                                                          ↓
                                                  PTT pre-key delay
                                                          ↓
                                              Radio broadcast via bus routing
```

---

## Troubleshooting

**TTS keys radio but plays silence**
- Check the routing page (`/routing`) — the playback source must be wired to an active radio bus. If the solo bus is connected to a disabled plugin (e.g. D75 off), audio won't reach the radio.

**"Kokoro model files missing"**
- Run `scripts/install.sh` to download them, or manually:
  ```bash
  BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
  mkdir -p tools/models/kokoro
  wget -O tools/models/kokoro/kokoro-v1.0.onnx "$BASE/kokoro-v1.0.onnx"
  wget -O tools/models/kokoro/voices-v1.0.bin  "$BASE/voices-v1.0.bin"
  ```

**Fallback to edge/gtts**
- Set `TTS_ENGINE = edge` or `TTS_ENGINE = gtts` in `gateway_config.txt`.
- Requires internet for both.

**"TTS not available"**
- `ENABLE_TTS = true` must be set.
- Kokoro: check `tools/models/kokoro/` contains both model files.
- edge/gtts: check internet connectivity and pip packages.

**Voice dropdown shows wrong voices after engine change**
- The dropdown is populated from the live status API — it updates within ~2 seconds of the status poll cycle.

**TTS sounds slow / words cut off**
- Try reducing `TTS_SPEED` (e.g. `1.0`) or increasing `PTT_TTS_DELAY` (e.g. `0.75`).

---

## Security Notes

Text commands have no authentication by default — any Mumble user can trigger TTS.  
For public servers, add a user whitelist in `on_text_message()` in `text_commands.py`:

```python
AUTHORIZED = ['W1XYZ', 'K2ABC']
if not any(call in sender_name for call in AUTHORIZED):
    return
```
