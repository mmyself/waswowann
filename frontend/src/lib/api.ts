import type {
  ApiErrorPayload,
  EscalationAction,
  MediaMeasurementPatch,
  MergeCandidate,
  GoogleAuthStatus,
  GooglePickerSession,
  ImportJob,
  MutationFeedback,
  ObservationDetail,
  ObservationListResponse,
  PeriodicityReport,
  PublicConfig,
  WorldlineInspection,
  ZukunftsgraphSnapshot,
} from "./types";

const ENV_API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");
export const API_BASE_URL =
  typeof window !== "undefined" && !["localhost", "127.0.0.1"].includes(window.location.hostname)
    ? ""
    : ENV_API_BASE_URL;

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(message: string, status: number, code = "API_ERROR") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (init.body && !isFormData && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ApiErrorPayload;
    const error = payload.error ?? payload.detail?.error;
    throw new ApiError(error?.message ?? `Request failed (${response.status})`, response.status, error?.code);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const mediaUrl = (url: string | null | undefined) => {
  if (!url) return null;
  if (!API_BASE_URL) return url;
  try {
    return new URL(url, `${API_BASE_URL}/`).toString();
  } catch {
    return null;
  }
};

export interface BrowserLocation {
  lat: number;
  lon: number;
  accuracy_meters: number;
  captured_at: string;
}

export interface UploadItemResult {
  filename: string;
  observation_id: string | null;
  media_id: string | null;
  result: "imported" | "duplicate" | "error";
  error_code?: string | null;
}

export interface UploadResponse { items: UploadItemResult[]; }

export const api = {
  health: () => request<{ status: string }>("/api/health"),
  config: () => request<PublicConfig>("/api/config"),
  observations: (signal?: AbortSignal) => request<ObservationListResponse>("/api/observations", { signal }),
  observation: (id: string, signal?: AbortSignal) => request<ObservationDetail>(`/api/observations/${encodeURIComponent(id)}`, { signal }),
  createObservation: (body: Pick<ObservationDetail, "title"> & Partial<Pick<ObservationDetail, "category" | "description" | "environment">>) =>
    request<ObservationDetail>("/api/observations", { method: "POST", body: JSON.stringify(body) }),
  updateObservation: (id: string, body: Record<string, unknown>) =>
    request<ObservationDetail>(`/api/observations/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),
  mergeCandidates: (id: string, signal?: AbortSignal) =>
    request<{ items: MergeCandidate[] }>(`/api/observations/${encodeURIComponent(id)}/merge-candidates`, { signal }),
  mergeObservation: (targetId: string, sourceObservationIds: string[]) =>
    request<ObservationDetail>(`/api/observations/${encodeURIComponent(targetId)}/merge`, {
      method: "POST",
      body: JSON.stringify({ source_observation_ids: sourceObservationIds }),
    }),
  updateMedia: (id: string, body: MediaMeasurementPatch) =>
    request<ObservationDetail>(`/api/media/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteMedia: (id: string) =>
    request<ObservationDetail>(`/api/media/${encodeURIComponent(id)}`, { method: "DELETE" }),
  uploadFiles: (
    files: File[],
    timezone: string,
    browserLocation?: BrowserLocation,
    options?: { targetObservationId?: string; groupSameLocation?: boolean }
  ) => {
    const body = new FormData();
    files.forEach((file) => body.append("files[]", file, file.name));
    body.append("timezone", timezone);
    if (browserLocation) body.append("browser_location", JSON.stringify(browserLocation));
    if (options?.targetObservationId) body.append("target_observation_id", options.targetObservationId);
    if (options?.groupSameLocation !== undefined) body.append("group_same_location", options.groupSameLocation ? "true" : "false");
    return request<UploadResponse>("/api/uploads", { method: "POST", body });
  },
  startAnalysis: (id: string, question?: string, parentNoteId?: string) =>
    request<{ note_id: string; status: string }>(`/api/observations/${encodeURIComponent(id)}/analysis`, {
      method: "POST", body: JSON.stringify({ question: question || undefined, parent_note_id: parentNoteId || undefined }),
    }),
  startDemoImport: () => request<{ job_id: string }>("/api/imports/demo", { method: "POST", body: "{}" }),
  startLocalImport: () => request<{ job_id: string }>("/api/imports/local", { method: "POST", body: "{}" }),
  importJob: (id: string, signal?: AbortSignal) => request<ImportJob>(`/api/imports/${encodeURIComponent(id)}`, { signal }),
  googleAuth: () => request<GoogleAuthStatus>("/api/google/auth/status"),
  googlePicker: () => request<GooglePickerSession>("/api/google/picker/sessions", { method: "POST", body: "{}" }),
  googlePickerStatus: (id: string, signal?: AbortSignal) => request<GooglePickerSession>(`/api/google/picker/sessions/${encodeURIComponent(id)}`, { signal }),
  importGooglePicker: (id: string) => request<{ job_id: string }>(`/api/google/picker/sessions/${encodeURIComponent(id)}/import`, { method: "POST", body: "{}" }),
  cancelGooglePicker: (id: string) => request<void>(`/api/google/picker/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  zukunftsgraphSnapshot: (signal?: AbortSignal) =>
    request<ZukunftsgraphSnapshot>("/api/zukunftsgraph/snapshot", { signal }),
  syncZukunftsgraph: (seedPlans = true) =>
    request<{ sync: Record<string, unknown>; snapshot: ZukunftsgraphSnapshot }>("/api/zukunftsgraph/sync", {
      method: "POST",
      body: JSON.stringify({ seed_plans: seedPlans }),
    }),
  worldline: (itemId: string, signal?: AbortSignal) =>
    request<WorldlineInspection>(`/api/zukunftsgraph/worldlines/${encodeURIComponent(itemId)}`, { signal }),
  recordMetaObservation: (payload: {
    entity_id: string;
    property_name: string;
    label?: string;
    value_num: number;
    value_sigma?: number;
    value_unit?: string;
    time_ms?: number;
  }) =>
    request<{ feedback: MutationFeedback; snapshot: ZukunftsgraphSnapshot }>("/api/zukunftsgraph/observations", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  createMetaGoal: (payload: {
    entity_id_or_obs_id: string;
    property_name?: string;
    label: string;
    target_min?: number;
    target_max?: number;
    target_latest_ms?: number;
    target_radius_meters?: number;
  }) =>
    request<{ goal: Record<string, unknown>; snapshot: ZukunftsgraphSnapshot }>("/api/zukunftsgraph/goals", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  resolveEscalation: (goalId: string, action: EscalationAction, params: Record<string, unknown> = {}) =>
    request<{ feedback: MutationFeedback; snapshot: ZukunftsgraphSnapshot }>(
      `/api/zukunftsgraph/escalations/${encodeURIComponent(goalId)}/resolve`,
      { method: "POST", body: JSON.stringify({ action, params }) }
    ),
  periods: (params?: { entity_id?: string; category?: string; property_name?: string; tidal_offset_hours?: number }, signal?: AbortSignal) => {
    const qs = new URLSearchParams();
    if (params?.entity_id) qs.set("entity_id", params.entity_id);
    if (params?.category) qs.set("category", params.category);
    if (params?.property_name) qs.set("property_name", params.property_name);
    if (params?.tidal_offset_hours !== undefined) qs.set("tidal_offset_hours", String(params.tidal_offset_hours));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<PeriodicityReport>(`/api/zukunftsgraph/periods${suffix}`, { signal });
  },
  analyzePeriods: (payload: { timestamps_ms: number[]; label?: string; tidal_offset_hours?: number; now_ms?: number }) =>
    request<PeriodicityReport>("/api/zukunftsgraph/periods/analyze", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};
