"use client";

import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { DateTime } from "luxon";
import { api, ApiError } from "@/lib/api";
import { DISPLAY_TIMEZONE } from "@/lib/time-filter";
import type { ChatMessage, ChatScopeType, ObservationDetail, ZukunftsgraphSnapshot } from "@/lib/types";
import MicButton from "./MicButton";

interface Props {
  scopeType: ChatScopeType;
  scopeId: string;
  entityTitle?: string;
  mode?: "full" | "compact";
  snapshot?: ZukunftsgraphSnapshot | null;
  onSnapshotChange?: (snapshot: ZukunftsgraphSnapshot) => void;
  onObservationUpdated?: (detail: ObservationDetail) => void;
  onWorldlineChanged?: () => void;
  onRefreshObservations?: () => void;
}

function formatChatTime(ms: number): string {
  return DateTime.fromMillis(ms, { zone: DISPLAY_TIMEZONE }).toFormat("HH:mm");
}

export default function ChatPanel({
  scopeType,
  scopeId,
  entityTitle,
  mode = "full",
  snapshot,
  onSnapshotChange,
  onObservationUpdated,
  onWorldlineChanged,
  onRefreshObservations,
}: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [draft, setDraft] = useState("");
  const [interimText, setInterimText] = useState("");
  const [autoSendVoice, setAutoSendVoice] = useState(false);
  const [error, setError] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  const suggestions =
    scopeType === "entity"
      ? [
          "Wie ist der aktuelle Stand & die Prognose?",
          "Notiere: heute kurz kontrolliert, alles unauffällig.",
          "Setze ein neues Ziel für dieses Ding.",
        ]
      : [
          "Was steht bei meinen Funden gerade als Nächstes an?",
          "Welche Ziele im Zukunftsgraphen brauchen Aufmerksamkeit?",
          "Fasse meinen aktuellen Weltlinien-Status zusammen.",
        ];

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api
      .chatMessages(scopeType, scopeId, controller.signal)
      .then((res) => setMessages(res.messages))
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof ApiError ? cause.message : "Chatverlauf konnte nicht geladen werden.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [scopeType, scopeId]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, sending]);

  const sendText = useCallback(
    async (rawText: string) => {
      const text = rawText.trim();
      if (!text || sending) return;
      setSending(true);
      setError("");
      setInterimText("");
      try {
        const res = await api.sendChatMessage({
          message: text,
          scope_type: scopeType,
          scope_id: scopeId,
        });
        setMessages((prev) => [...prev, res.user_message, res.assistant_message]);
        setDraft("");
        onSnapshotChange?.(res.snapshot);
        if (res.updated_observation) onObservationUpdated?.(res.updated_observation);
        if (res.executed_actions.some((a) => a.ok)) {
          onWorldlineChanged?.();
          onRefreshObservations?.();
        }
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : "Nachricht konnte nicht gesendet werden.");
      } finally {
        setSending(false);
      }
    },
    [scopeType, scopeId, sending, onSnapshotChange, onObservationUpdated, onWorldlineChanged, onRefreshObservations]
  );

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void sendText(draft);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendText(draft);
    }
  };

  const handleTranscript = (text: string, isFinal: boolean) => {
    if (!isFinal) {
      setInterimText(text);
      return;
    }
    setInterimText("");
    const clean = text.trim();
    if (!clean) return;
    if (autoSendVoice && !draft.trim()) {
      void sendText(clean);
    } else {
      setDraft((prev) => (prev.trim() ? `${prev.trim()} ${clean}` : clean));
    }
  };

  const handleClear = async () => {
    if (sending) return;
    setError("");
    try {
      await api.clearChatMessages(scopeType, scopeId);
      setMessages([]);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Verlauf konnte nicht geleert werden.");
    }
  };

  return (
    <section
      className={`chat-panel chat-panel--${mode}`}
      aria-label={scopeType === "entity" ? `Chat zu ${entityTitle ?? "diesem Ding"}` : "WasWoWann Chat & Sprache"}
    >
      <div className="chat-header">
        <div>
          <p className="eyebrow">{scopeType === "entity" ? "Dialog zu diesem Ding" : "WasWoWann Assistent"}</p>
          <h3>{scopeType === "entity" ? `Sprechen & chatten: ${entityTitle ?? "Objekt"}` : "Chat & Sprache mit dem Weltmodell"}</h3>
          {mode === "full" && snapshot && (
            <small className="chat-subline">
              {snapshot.synced_observation_count} Funde · {snapshot.entities.length} Weltlinien · {snapshot.goal_evaluations.length} Ziele im Kontext
            </small>
          )}
        </div>
        {messages.length > 0 && (
          <button type="button" className="text-action" disabled={sending} onClick={handleClear}>
            Verlauf leeren
          </button>
        )}
      </div>

      <div className="chat-messages" ref={listRef} role="log" aria-live="polite">
        {loading ? (
          <p className="chat-empty-hint">Verlauf wird geladen …</p>
        ) : messages.length === 0 ? (
          <div className="chat-empty-state">
            <p className="chat-empty-hint">
              {scopeType === "entity"
                ? "Frag nach diesem Ding oder sprich per Mikrofon eine Notiz, Messung oder ein neues Ziel ein."
                : "Frag nach deinen Funden, Zielen und Prognosen — oder diktiere per Mikrofon eine neue Beobachtung."}
            </p>
            <div className="chat-suggestions">
              {suggestions.map((item) => (
                <button
                  key={item}
                  type="button"
                  className="chat-suggestion-chip"
                  disabled={sending}
                  onClick={() => void sendText(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((msg) => (
            <article key={msg.id} className={`chat-bubble chat-bubble--${msg.role}`}>
              <div className="chat-bubble__meta">
                <span>{msg.role === "user" ? "Du" : "WasWoWann"}</span>
                <time>{formatChatTime(msg.created_at_ms)}</time>
              </div>
              <p className="chat-bubble__text">{msg.content}</p>
              {msg.actions && msg.actions.length > 0 && (
                <div className="chat-actions">
                  {msg.actions.map((act, idx) => (
                    <span
                      key={`${msg.id}-act-${idx}`}
                      className={`chat-action-badge ${act.ok ? "is-ok" : "is-error"}`}
                    >
                      {act.ok ? "✓" : "!"} {act.summary}
                    </span>
                  ))}
                </div>
              )}
            </article>
          ))
        )}
        {sending && (
          <article className="chat-bubble chat-bubble--assistant is-pending">
            <div className="chat-bubble__meta"><span>WasWoWann</span></div>
            <p className="chat-bubble__text">Denke nach …</p>
          </article>
        )}
      </div>

      {interimText && (
        <div className="chat-interim" aria-live="polite">
          <span>Gehört:</span> „{interimText}“
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}

      <form className="chat-composer" onSubmit={handleSubmit}>
        <textarea
          rows={mode === "compact" ? 2 : 3}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={sending}
          placeholder={
            scopeType === "entity"
              ? "Zu diesem Ding etwas fragen, notieren oder einsprechen …"
              : "Frage stellen, Messwert durchgeben oder Ziel besprechen …"
          }
        />
        <div className="chat-composer__controls">
          <div className="chat-composer__left">
            <MicButton
              compact={mode === "compact"}
              disabled={sending}
              label={scopeType === "entity" ? "Zu diesem Ding sprechen" : "Mikrofon starten"}
              onTranscript={handleTranscript}
              onError={setError}
            />
            <label className="chat-voice-autosend">
              <input
                type="checkbox"
                checked={autoSendVoice}
                onChange={(e) => setAutoSendVoice(e.target.checked)}
              />
              Direkt senden
            </label>
          </div>
          <button type="submit" className="save-button" disabled={sending || !draft.trim()}>
            Senden <span aria-hidden="true">↗</span>
          </button>
        </div>
      </form>
    </section>
  );
}
