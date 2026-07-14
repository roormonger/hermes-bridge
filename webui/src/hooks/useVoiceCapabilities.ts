import { useEffect, useState } from "react";
import { getVoiceDeps } from "../api";

export interface VoiceCapabilities {
  ttsAvailable: boolean;
  sttAvailable: boolean;
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
        setCaps({
          ttsAvailable: !missingStr.includes("edge-tts"),
          sttAvailable: !missingStr.includes("faster-whisper") && !missingStr.includes("imageio-ffmpeg"),
        });
      })
      .catch(() => {
        setCaps({ ttsAvailable: false, sttAvailable: false });
      });
  }, []);

  return caps;
}
