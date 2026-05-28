"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

import collections
import json as json_mod
import math as _math_mod
import os
import queue as _queue_mod
import re
import socket
import struct
import subprocess
import sys
import threading
import time

try:
    from pymumble_py3 import Mumble
    from pymumble_py3.callbacks import (
        PYMUMBLE_CLBK_SOUNDRECEIVED,
        PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
    )
except ImportError:
    from pymumble import Mumble
    from pymumble.callbacks import (
        PYMUMBLE_CLBK_SOUNDRECEIVED,
        PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
    )


class _SetupAudioMumbleMixin:
    def setup_audio(self):
        """Initialize plugins, audio sources, and external services.

        Each phase is a function in gateway_setup.py. They run in order
        because some have dependencies (SDR before TH-9800 for the fork
        safety constraint; TH-9800 before CAT connect because the plugin
        produces the cat_client; tunnel before GDrive because GDrive
        publishes the tunnel URL on startup).

        Phases catch their own exceptions and leave the relevant attribute
        as None on failure — they don't raise. The single outer try/except
        is a last-resort safety net for the orchestrator itself.
        """
        if self.config.VERBOSE_LOGGING:
            print("Initializing audio...")
        try:
            import gateway_setup as gs
            gs.setup_sdr(self)
            gs.setup_th9800(self)
            gs.setup_playback(self)
            gs.setup_tts(self)
            gs.setup_remote_audio(self)
            gs.setup_announce_input(self)
            gs.setup_web_audio(self)
            # setup_gateway_link must run BEFORE kv4p loopback endpoints —
            # the endpoints connect to the link server at 127.0.0.1:9700.
            gs.setup_gateway_link(self)
            gs.setup_kv4p_loopback_endpoints(self)
            gs.setup_packet(self)
            gs.setup_mumble_servers(self)
            gs.setup_smart_announce(self)
            gs.setup_web_config(self)
            gs.setup_manager_engine(self)
            gs.setup_alert_engine(self)
            gs.setup_ddns(self)
            gs.setup_cloudflare_tunnel(self)
            gs.setup_supervised_streamers(self)
            gs.setup_email(self)
            gs.setup_gdrive(self)
            gs.setup_gps(self)
            gs.setup_repeaters(self)
            gs.setup_echolink(self)
            gs.setup_streaming(self)
            gs.setup_cat_connect(self)
            return True
        except Exception as e:
            print(f"✗ Could not initialize audio: {e}")
            import traceback; traceback.print_exc()
            return False

    def setup_mumble(self):
        """Initialize Mumble connection"""

        if self.secondary_mode:
            print()
            print("=" * 60)
            print("  SECONDARY MODE — this machine is not the active gateway")
            print("  Reason: Broadcastify feed already live on another server")
            print("  Mumble: DISABLED (username would conflict)")
            print("  DarkIce: DISABLED (mountpoint already occupied)")
            print("  Audio bridge (FFmpeg/loopback) still running.")
            print("=" * 60)
            return True

        # Create MumbleSource for routing system
        from audio_sources import MumbleSource
        self.mumble_source = MumbleSource(self.config, gateway=self)
        print(f"\nConnecting to Mumble: {self.config.MUMBLE_SERVER}:{self.config.MUMBLE_PORT}...")

        try:
            # Create Mumble client
            print(f"  Creating Mumble client...")
            self.mumble = Mumble(
                self.config.MUMBLE_SERVER, 
                self.config.MUMBLE_USERNAME,
                port=self.config.MUMBLE_PORT,
                password=self.config.MUMBLE_PASSWORD if self.config.MUMBLE_PASSWORD else '',
                reconnect=False,  # pymumble reconnect causes ghost cycling on local servers
                stereo=self.config.MUMBLE_STEREO,
                debug=self.config.MUMBLE_DEBUG
            )
            
            # Set loop rate for low latency
            self.mumble.set_loop_rate(self.config.MUMBLE_LOOP_RATE)
            
            # Set up callback for received audio
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self.sound_received_handler)
            
            # Set up callback for text messages
            if self.config.ENABLE_TEXT_COMMANDS:
                try:
                    self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_TEXTMESSAGERECEIVED, self.on_text_message)
                    print("✓ Text message callback registered")
                    print("  Send text commands in Mumble chat (e.g., !status, !help)")
                except Exception as callback_err:
                    print(f"⚠ Text callback registration failed: {callback_err}")
            else:
                print("  Text commands: DISABLED (set ENABLE_TEXT_COMMANDS = true to enable)")
            
            # Enable receiving sound
            self.mumble.set_receive_sound(True)
            
            # Connect
            print(f"  Starting Mumble connection...")
            self.mumble.start()
            
            print(f"  Waiting for Mumble to be ready...")
            self.mumble.is_ready()
            
            print(f"✓ Connected as '{self.config.MUMBLE_USERNAME}'")
            
            # Wait for codec to initialize
            print("  Waiting for audio codec to initialize...")
            max_wait = 5  # seconds
            wait_start = time.time()
            while time.time() - wait_start < max_wait:
                if hasattr(self.mumble.sound_output, 'encoder_framesize') and self.mumble.sound_output.encoder_framesize is not None:
                    print(f"  ✓ Audio codec ready (framesize: {self.mumble.sound_output.encoder_framesize})")

                    break
                time.sleep(0.1)
            else:
                print("  ⚠ Audio codec not initialized after 5s")
                print("    Audio may not work until codec is ready")
                print("    This usually resolves itself within 10-30 seconds")

            # Increase audio_per_packet to bundle more frames per Mumble packet.
            # Default 0.02 (20ms = 1 frame/packet) causes stutter when pymumble's
            # loop is GIL-starved (only fires ~20x/sec instead of 50x/sec).
            # At 0.06 (60ms = 3 frames/packet), 20 sends/sec × 60ms = 1200ms/sec.
            try:
                self.mumble.sound_output.set_audio_per_packet(0.06)
                print(f"  Mumble audio_per_packet set to 0.06 (60ms, 3 frames/packet)")
            except Exception as e:
                print(f"  ⚠ Could not set audio_per_packet: {e}")

            # Apply audio quality settings now that the codec is ready.
            # set_bandwidth() was never called before — the library default is 50kbps.
            # complexity=10: max Opus quality (marginal CPU cost on Pi)
            # signal=3001: OPUS_SIGNAL_VOICE — tunes psychoacoustic model for speech
            try:
                self.mumble.set_bandwidth(self.config.MUMBLE_BITRATE)
                enc = getattr(self.mumble.sound_output, 'encoder', None)
                if enc is not None:
                    enc.vbr = 1 if self.config.MUMBLE_VBR else 0
                    enc.complexity = 10
                    enc.signal = 3001  # OPUS_SIGNAL_VOICE
                    print(f"  ✓ Opus encoder: {self.config.MUMBLE_BITRATE//1000}kbps, "
                          f"VBR={'on' if self.config.MUMBLE_VBR else 'off'}, "
                          f"complexity=10, signal=voice")
                else:
                    print(f"  ✓ Mumble bandwidth set to {self.config.MUMBLE_BITRATE//1000}kbps "
                          f"(VBR will apply when codec negotiates)")
            except Exception as qe:
                print(f"  ⚠ Could not apply audio quality settings: {qe}")

            # Join channel if specified
            if self.config.MUMBLE_CHANNEL:
                try:
                    print(f"  Joining channel: {self.config.MUMBLE_CHANNEL}")
                    channel = self.mumble.channels.find_by_name(self.config.MUMBLE_CHANNEL)
                    if channel:
                        channel.move_in()
                        print(f"  ✓ Joined channel: {self.config.MUMBLE_CHANNEL}")
                    else:
                        print(f"  ⚠ Channel '{self.config.MUMBLE_CHANNEL}' not found")
                        print(f"    Staying in root channel")
                except Exception as ch_err:
                    print(f"  ✗ Could not join channel: {ch_err}")
            
            if self.config.VERBOSE_LOGGING:
                print(f"  Loop rate: {self.config.MUMBLE_LOOP_RATE}s ({1/self.config.MUMBLE_LOOP_RATE:.0f} Hz)")
            
            return True
            
        except Exception as e:
            if 'already in use' in str(e).lower() or 'username already' in str(e).lower():
                self.secondary_mode = True
                print()
                print("=" * 60)
                print("  SECONDARY MODE — this machine is not the active gateway")
                print(f"  Reason: Mumble username '{self.config.MUMBLE_USERNAME}' already connected")
                print("  Mumble: DISABLED (username conflict)")
                print("  Hint: DarkIce may also fail if the Broadcastify feed is already live.")
                print("=" * 60)
                return True
            print(f"\n✗ MUMBLE CONNECTION FAILED: {e}")
            print(f"\n  Configuration:")
            print(f"    Server: {self.config.MUMBLE_SERVER}")
            print(f"    Port: {self.config.MUMBLE_PORT}")
            print(f"    Username: {self.config.MUMBLE_USERNAME}")
            print(f"\n  Please check:")
            print(f"  1. Is the Mumble server running?")
            print(f"  2. Is the IP address correct in gateway_config.txt?")
            print(f"  3. Is the port correct? (default: 64738)")
            print(f"  4. Can you connect with the official Mumble client?")
            print(f"\n  Test with Mumble client first:")
            print(f"    Server: {self.config.MUMBLE_SERVER}")
            print(f"    Port: {self.config.MUMBLE_PORT}")
            return False
    
    # gTTS voice map: number → (lang, tld, description)
    # gTTS voices (Google Translate, robotic but reliable)
    TTS_VOICES = {
        1: ('en', 'com',    'US English'),
        2: ('en', 'co.uk',  'British English'),
        3: ('en', 'com.au', 'Australian English'),
        4: ('en', 'co.in',  'Indian English'),
        5: ('en', 'co.za',  'South African English'),
        6: ('en', 'ca',     'Canadian English'),
        7: ('en', 'ie',     'Irish English'),
        8: ('fr', 'fr',     'French'),
        9: ('de', 'de',     'German'),
    }

    # Edge TTS voices (Microsoft Neural, natural sounding)
    EDGE_TTS_VOICES = {
        1: ('en-US-AndrewNeural',    'US English (Andrew)'),
        2: ('en-GB-RyanNeural',      'British English (Ryan)'),
        3: ('en-AU-WilliamMultilingualNeural', 'Australian English (William)'),
        4: ('en-IN-PrabhatNeural',   'Indian English (Prabhat)'),
        5: ('en-US-GuyNeural',       'US English (Guy)'),
        6: ('en-CA-LiamNeural',      'Canadian English (Liam)'),
        7: ('en-IE-ConnorNeural',    'Irish English (Connor)'),
        8: ('en-US-AvaNeural',       'US English (Ava)'),
        9: ('en-US-EmmaNeural',      'US English (Emma)'),
    }

