from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import httpx

from app.config import Settings, settings

@dataclass
class AnalysisResult:
    title: str
    category: str
    description: str
    scientific_name: str | None
    confidence: float | None
    tags: list[str]
    alternatives: list[dict]
    questions: list[str]
    warnings: list[str]
    raw_text: str
    estimated_sigma_meters: float | None = None
    estimated_sigma_seconds: float | None = None
    uncertainty_rationale: str | None = None
    environment: str | None = None
    apply_correction: bool = False


def _bounded_positive(value, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        return None
    return max(low, min(high, numeric))


def _parse(text: str) -> AnalysisResult:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        data=json.loads(cleaned)
    except (ValueError,TypeError) as exc:
        raise ValueError("ANALYSIS_SCHEMA_INVALID") from exc
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data,dict): raise ValueError("ANALYSIS_SCHEMA_INVALID")
    title=data.get('title','Unbekannt'); category=data.get('category','Unbekannt'); description=data.get('description','')
    if not isinstance(title,str) or not isinstance(category,str) or not isinstance(description,str): raise ValueError('ANALYSIS_SCHEMA_INVALID')
    confidence=data.get('confidence')
    if confidence is not None and (not isinstance(confidence,(int,float)) or not 0<=confidence<=1): raise ValueError('ANALYSIS_SCHEMA_INVALID')
    for key in ('tags','alternatives','questions','warnings'):
        if not isinstance(data.get(key,[]),list): raise ValueError('ANALYSIS_SCHEMA_INVALID')
    sigma_m = _bounded_positive(data.get('estimated_sigma_meters'), 1.0, 50_000.0)
    sigma_s = _bounded_positive(data.get('estimated_sigma_seconds'), 1.0, 30 * 86_400.0)
    rationale = data.get('uncertainty_rationale')
    if not isinstance(rationale, str) or not rationale.strip():
        rationale = None
    env_raw = data.get('environment')
    environment = env_raw.strip().lower() if isinstance(env_raw, str) else None
    if environment not in {'indoor', 'outdoor', 'unknown'}:
        environment = None
    apply_correction = data.get('apply_correction') is True
    return AnalysisResult(
        title[:200],
        category[:32],
        description[:10000],
        data.get('scientific_name'),
        confidence,
        list(data.get('tags',[])),
        list(data.get('alternatives',[])),
        list(data.get('questions',[])),
        list(data.get('warnings',[])),
        text,
        sigma_m,
        sigma_s,
        rationale[:500] if rationale else None,
        environment,
        apply_correction,
    )


def analyze(
    image_bytes: bytes,
    mime_type: str = 'image/jpeg',
    context: str = '',
    question: str | None = None,
    *,
    config: Settings = settings,
    images: list[tuple[bytes, str]] | None = None,
) -> AnalysisResult:
    image_list = images if images else [(image_bytes, mime_type)]
    prompt=(
        "Du bist ein sorgfältiger naturkundlicher und objektbezogener Bildanalyst. "
        "Bestimme das abgebildete Objekt anhand aller beigefügten Fotos sowie des Raum-/Zeit-Kontexts möglichst präzise und ausführlich auf Deutsch. "
        "Antworte ausschließlich als einzelnes JSON-Objekt mit genau diesen Feldern:\n"
        "- title: prägnanter deutscher Name (bei eindeutiger Tendenz konkrete Art + Gattung, z. B. 'Nebelkappe (Clitocybe nebularis)' oder 'Igel-Stachelbart')\n"
        "- category: genau eines von 'Pilz', 'Pflanze', 'Person', 'Ding', 'Ereignis', 'Unbekannt'\n"
        "- description: ausführliche, klar strukturierte deutsche Analyse mit Absätzen (für Pilze/Pflanzen detailliert zu: 1. Hut/Blüte/Habitus, Farbe, Oberfläche & Rand; 2. Unterseite/Lamellen/Poren & Stielansatz wie herablaufend/angewachsen/frei; 3. Stiel, Fleisch & Substrat; 4. Ökologie, Jahreszeit & konkrete Abgrenzung zu Verwechslungspartnern)\n"
        "- scientific_name: lateinischer Art- oder Gattungsname oder null\n"
        "- confidence: Zahl 0..1 oder null\n"
        "- tags: Array relevanter deutscher Stichworte (z. B. Gattung, Lebensraum, Jahreszeit)\n"
        "- alternatives: Array mit 2-4 Objekten {name, scientific_name, confidence, rationale} (nenn konkrete Verwechslungspartner und woran man den Unterschied sieht)\n"
        "- questions: Array mit 2-4 konkreten Prüfmerkmalen vor Ort (z. B. Geruch, Lamellenbruch/Milchsaft, Sporenpulver, Begleitbäume)\n"
        "- warnings: Array mit wichtigen Sicherheitshinweisen (z. B. keine Verzehrfreigabe, giftige Verwechslungspartner explizit benennen)\n"
        "- environment: 'indoor' (Wohnung/Innenraum/Gebäude), 'outdoor' (im Freien), oder 'unknown'\n"
        "- apply_correction: Boolean (true, falls eine Rückfrage eine Korrektur/Umbenennung/Umgebungsvorgabe enthält; sonst false)\n"
        "- estimated_sigma_meters: positive Zahl in Metern (grobe GPS-Unsicherheit anhand Umgebung: z. B. 8-20 im Freien, 4-30 in Gebäudenähe/Wohnung)\n"
        "- estimated_sigma_seconds: positive Zahl in Sekunden (grobe Zeitunsicherheit: z. B. 10-60 bei Handyfoto)\n"
        "- uncertainty_rationale: 1 kurzer deutscher Satz zur Unsicherheit.\n"
        "Keine Verzehrfreigabe. Unbekannt ist zulässig. "
        + (f"Kontext: {context}. " if context else "")
        + (f"Rückfrage: {question}. " if question else "")
    )
    if config.gemini_provider=='antigravity':
        if not config.antigravity_bridge_url or not config.antigravity_bridge_auth: raise RuntimeError('ANALYSIS_NOT_CONFIGURED')
        content = [{'type': 'text', 'text': prompt}] + [
            {'type': 'image_url', 'image_url': {'url': f'data:{mtype};base64,{base64.b64encode(ibytes).decode("ascii")}'}}
            for ibytes, mtype in image_list
        ]
        payload={'model':config.gemini_model,'messages':[{'role':'user','content':content}],'stream':False}
        url=config.antigravity_bridge_url.rstrip('/')
        if url.endswith('/v1'): url += '/chat/completions'
        else: url += '/v1/chat/completions'
        response=httpx.post(url,json=payload,headers={'Authorization':f'Bearer {config.antigravity_bridge_auth}'},timeout=125)
        response.raise_for_status(); body=response.json()
        text=body['choices'][0]['message']['content']
        if not isinstance(text,str): raise ValueError('ANALYSIS_SCHEMA_INVALID')
        return _parse(text)
    if config.gemini_provider!='google' or not config.google_api_key: raise RuntimeError('ANALYSIS_NOT_CONFIGURED')
    url=f'https://generativelanguage.googleapis.com/v1beta/models/{config.gemini_model}:generateContent'
    parts = [{'text': prompt}] + [
        {'inline_data': {'mime_type': mtype, 'data': base64.b64encode(ibytes).decode('ascii')}}
        for ibytes, mtype in image_list
    ]
    payload={'contents':[{'parts':parts}],'generationConfig':{'responseMimeType':'application/json','temperature':0.2}}
    response=httpx.post(url,params={'key':config.google_api_key},json=payload,timeout=90)
    response.raise_for_status(); body=response.json()
    text=body['candidates'][0]['content']['parts'][0]['text']
    return _parse(text)
