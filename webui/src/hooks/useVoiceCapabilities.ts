import { useEffect, useState } from "react";
import { getVoiceConfig } from "../api";

export interface VoiceCapabilities {
  ttsAvailable: boolean;
  sttAvailable: boolean;
  ttsVoice?: string;
}

export function useVoiceCapabilities(): VoiceCapabilities {
  const [caps, setCaps] = useState<VoiceCapabilities>({ ttsAvailable: true, sttAvailable: true });

  useEffect(() => {
    getVoiceConfig()
      .then((data: any) => {
        const enabled: boolean = data?.voice_enabled !== false;
        setCaps((prev) => ({
          ttsAvailable: enabled && data?.tts_available !== false,
          sttAvailable: enabled && data?.stt_available !== false,
          ttsVoice: prev.ttsVoice,
        }));
      })
      .catch(() => {
        // On error leave optimistic values as-is so buttons stay visible
        setCaps((prev) => prev);
      });
  }, []);

  return caps;
}
