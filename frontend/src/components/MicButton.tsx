"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";

interface SpeechRecognitionAlternativeLike {
  transcript: string;
}

interface SpeechRecognitionResultLike {
  isFinal: boolean;
  length: number;
  [index: number]: SpeechRecognitionAlternativeLike;
}

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResultLike>;
}

interface SpeechRecognitionErrorEventLike {
  error?: string;
}

interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

function canMediaRecord(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof navigator !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    typeof MediaRecorder !== "undefined"
  );
}

interface Props {
  onTranscript: (text: string, isFinal: boolean) => void;
  onError?: (message: string) => void;
  disabled?: boolean;
  compact?: boolean;
  label?: string;
}

export default function MicButton({
  onTranscript,
  onError,
  disabled = false,
  compact = false,
  label = "Sprechen",
}: Props) {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [supported, setSupported] = useState(true);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  useEffect(() => {
    setSupported(Boolean(getSpeechRecognitionCtor()) || canMediaRecord());
  }, []);

  useEffect(() => {
    return () => {
      try {
        recognitionRef.current?.stop();
      } catch {
        // Ignore stop errors on unmount
      }
      if (recorderRef.current && recorderRef.current.state !== "inactive") {
        try {
          recorderRef.current.stop();
        } catch {
          // Ignore
        }
      }
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  const stopRecording = () => {
    if (recognitionRef.current) {
      try {
        recognitionRef.current.stop();
      } catch {
        // Ignore
      }
      recognitionRef.current = null;
      setRecording(false);
    }
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      try {
        recorderRef.current.stop();
      } catch {
        // Ignore
      }
    }
  };

  const startWebSpeech = (Ctor: SpeechRecognitionCtor) => {
    const rec = new Ctor();
    rec.lang = "de-DE";
    rec.interimResults = true;
    rec.continuous = false;

    let finalAccum = "";
    rec.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const res = event.results[i];
        const part = res[0]?.transcript ?? "";
        if (res.isFinal) {
          finalAccum = `${finalAccum} ${part}`.trim();
          onTranscript(part.trim(), true);
        } else {
          interim = `${interim} ${part}`.trim();
        }
      }
      if (interim) {
        onTranscript(interim, false);
      }
    };

    rec.onerror = (event) => {
      const code = event.error || "";
      if (code === "not-allowed" || code === "service-not-allowed") {
        onError?.("Mikrofon-Zugriff wurde blockiert. Bitte erlaube das Mikrofon im Browser.");
      } else if (code === "no-speech") {
        onError?.("Keine Sprache erkannt. Bitte erneut versuchen.");
      } else if (code && code !== "aborted") {
        onError?.(`Spracherkennung fehlgeschlagen (${code}).`);
      }
      setRecording(false);
      recognitionRef.current = null;
    };

    rec.onend = () => {
      setRecording(false);
      recognitionRef.current = null;
    };

    recognitionRef.current = rec;
    setRecording(true);
    rec.start();
  };

  const startMediaRecorderFallback = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;

      recorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) chunksRef.current.push(ev.data);
      };

      recorder.onstop = async () => {
        setRecording(false);
        stream.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        recorderRef.current = null;
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        chunksRef.current = [];
        if (!blob.size) return;
        setTranscribing(true);
        try {
          const res = await api.transcribeAudio(blob, "voice.webm");
          if (res.text?.trim()) onTranscript(res.text.trim(), true);
        } catch (cause) {
          onError?.(cause instanceof ApiError ? cause.message : "Audio-Transkription fehlgeschlagen.");
        } finally {
          setTranscribing(false);
        }
      };

      setRecording(true);
      recorder.start();
    } catch {
      setRecording(false);
      onError?.("Mikrofon konnte nicht geöffnet werden. Bitte Berechtigung im Browser prüfen.");
    }
  };

  const toggle = () => {
    if (disabled || transcribing) return;
    if (recording) {
      stopRecording();
      return;
    }
    onError?.("");
    const Ctor = getSpeechRecognitionCtor();
    if (Ctor) {
      startWebSpeech(Ctor);
      return;
    }
    if (canMediaRecord()) {
      void startMediaRecorderFallback();
      return;
    }
    onError?.("Dieses Gerät/dieser Browser unterstützt keine Mikrofon-Aufnahme.");
  };

  const buttonText = transcribing
    ? "Verarbeite …"
    : recording
    ? compact
      ? "Stopp"
      : "Höre zu … (Stopp)"
    : compact
    ? "Mikrofon"
    : label;

  return (
    <button
      type="button"
      className={`mic-button ${compact ? "is-compact" : ""} ${recording ? "is-recording" : ""}`}
      disabled={disabled || transcribing || !supported}
      aria-pressed={recording}
      aria-label={recording ? "Sprachaufnahme beenden" : label}
      title={supported ? label : "Spracherkennung in diesem Browser nicht verfügbar"}
      onClick={toggle}
    >
      <span className="mic-button__dot" aria-hidden="true" />
      <span>{buttonText}</span>
    </button>
  );
}
