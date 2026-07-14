import { useCallback, useState } from "react";

const STORAGE_KEY = "hermes_tts_voice";

export const DEFAULT_VOICE = "en-US-AriaNeural";

export const TTS_VOICES: { label: string; value: string; description: string }[] = [
  { label: "Aria (US)", value: "en-US-AriaNeural", description: "Female · Conversational" },
  { label: "Jenny (US)", value: "en-US-JennyNeural", description: "Female · Friendly" },
  { label: "Guy (US)", value: "en-US-GuyNeural", description: "Male · Conversational" },
  { label: "Eric (US)", value: "en-US-EricNeural", description: "Male · Natural" },
  { label: "Sonia (UK)", value: "en-GB-SoniaNeural", description: "Female · British" },
  { label: "Ryan (UK)", value: "en-GB-RyanNeural", description: "Male · British" },
  { label: "Natasha (AU)", value: "en-AU-NatashaNeural", description: "Female · Australian" },
  { label: "William (AU)", value: "en-AU-WilliamNeural", description: "Male · Australian" },
];

function readStored(): string {
  try { return localStorage.getItem(STORAGE_KEY) || DEFAULT_VOICE; } catch { return DEFAULT_VOICE; }
}

export function useTTSVoice() {
  const [voice, setVoiceState] = useState<string>(readStored);

  const setVoice = useCallback((v: string) => {
    try { localStorage.setItem(STORAGE_KEY, v); } catch {}
    setVoiceState(v);
  }, []);

  return { voice, setVoice };
}
