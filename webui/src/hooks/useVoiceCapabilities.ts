import { useEffect, useState } from "react";
import { getVoiceDeps } from "../api";

export interface VoiceCapabilities {
  ttsAvailable: boolean;
  sttAvailable: boolean;
  ttsVoice?: string;
}

export function useVoiceCapabilities(): VoiceCapabilities {
  const [caps, setCaps] = useState<VoiceCapabilities>({ ttsAvailable: true, sttAvailable: true });

  useEffect(() => {
    getVoiceDeps()
      .then((data: any) => {
        const missing: string[] = (data?.missing_optional ?? []).map((d: any) =>
          typeof d === "string" ? d : d.requirement ?? ""
        );
        const missingStr = missing.join(" ");
        setCaps((prev) => ({
          ttsAvailable: !missingStr.includes("edge-tts"),
          sttAvailable: !missingStr.includes("faster-whisper") && !missingStr.includes("imageio-ffmpeg"),
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
