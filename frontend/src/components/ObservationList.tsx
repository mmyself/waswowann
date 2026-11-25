"use client";

import { formatDateSpan } from "@/lib/time-filter";
import type { ObservationSummary } from "@/lib/types";

interface Props {
  observations: ObservationSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  total: number;
}

export default function ObservationList({ observations, selectedId, onSelect, total }: Props) {
  return <section className="observation-list" aria-label="Beobachtungsliste">
    <details open>
      <summary><span>Alle Funde</span><span className="list-count">{observations.length} / {total}</span></summary>
      {observations.length ? <div className="list-items">
        {observations.map((item, index) => <button type="button" key={item.id} className={`observation-row ${selectedId === item.id ? "is-selected" : ""}`} onClick={() => onSelect(item.id)}>
          <span className="row-index">{String(index + 1).padStart(2, "0")}</span>
          <span className="row-copy"><strong>{item.title || "Unbenannter Fund"}</strong><span>{formatDateSpan(item.first_observed_at, item.last_observed_at)} · {item.category}</span></span>
          <span className="row-badges"><span className="row-tag">{item.media_count} {item.media_count === 1 ? "Foto" : "Fotos"}</span>{(item.lat === null || item.lon === null) && <span className="row-tag">Ort fehlt</span>}</span>
          <span className="row-arrow" aria-hidden="true">↗</span>
        </button>)}
      </div> : <p className="list-empty">In diesem Zeitraum wurden keine Funde erfasst.</p>}
    </details>
  </section>;
}
