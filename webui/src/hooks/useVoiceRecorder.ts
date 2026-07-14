import { useCallback, useRef, useState } from "react";
import { transcribeAudio } from "../api";

export function useVoiceRecorder(onTranscript: (text: string) => void) {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  const start = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = async () => {
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        if (blob.size === 0) return;
        setTranscribing(true);
        try {
          const text = await transcribeAudio(blob);
          if (text) onTranscript(text);
        } catch (e) {
          console.error("Transcription failed:", e);
        } finally {
          setTranscribing(false);
        }
      };
      recorder.start();
      mediaRecorderRef.current = recorder;
      setRecording(true);
    } catch (e) {
      console.error("Failed to start recording:", e);
    }
  }, [onTranscript]);

  const stop = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    recorder.stop();
    mediaRecorderRef.current = null;
    setRecording(false);
  }, []);

  return { recording, transcribing, start, stop };
}
