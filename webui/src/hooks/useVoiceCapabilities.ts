import { useEffect, useState } from "react";
import { getVoiceDeps } from "../api";

export interface VoiceCapabilities {
  ttsAvailable: boolean;
  sttAvailable: boolean;
  ttsVoice?: string;
}

export function useVoiceCapabilities(): VoiceCapabilities {
  const [caps, setCaps] = useState<VoiceCapabilities>({ ttsAvailable: false, sttAvailable: false });

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
        setCaps((prev) => ({ ttsAvailable: false, sttAvailable: false, ttsVoice: prev.ttsVoice }));
      });
  }, []);

  return caps;
}
