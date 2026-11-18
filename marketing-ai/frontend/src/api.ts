export type Source = {
  number: number;
  document_id: string;
  name: string;
  location: string;
  excerpt: string;
};

export type Message = { id: number; role: 'user' | 'assistant'; text: string; sources: Source[] };
export type DocumentRecord = {
  id: string;
  name: string;
  metadata: Record<string, string | number | string[]> & {
    format: string;
    size_bytes: number;
    uploaded_at: string;
    passages: number;
  };
};
export type Workspace = {
  documents: DocumentRecord[];
  messages: Message[];
  answer_mode: 'excerpts' | 'local-model';
};
export type UploadResult = {
  added: number;
  errors: { name: string; detail: string }[];
  documents: DocumentRecord[];
};

export async function request<T>(path: string, init?: RequestInit): Promise<T>;
export async function request<T>(path: string, init?: RequestInit): Promise<T | undefined> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new Error('Could not reach the workspace. Check that the backend is running.');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === 'string' ? body.detail : `The request failed (${response.status}).`);
  }
  if (response.status !== 204) return response.json();
}
