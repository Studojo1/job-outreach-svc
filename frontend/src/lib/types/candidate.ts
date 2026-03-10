export interface ResumePreview {
  name?: string;
  email?: string;
  phone?: string;
  skills?: string[];
  education?: string[];
  experience_years?: number;
  summary?: string;
  char_count?: number;
}

export interface MCQOption {
  label: string;
  text: string;
}

export interface MCQQuestion {
  question: string;
  options: MCQOption[];
  allow_multiple: boolean;
}

export interface AgentResponse {
  message: string;
  current_state: string;
  mcq: MCQQuestion | null;
  text_input: boolean;
  is_complete: boolean;
  questions_asked_so_far: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface CandidateProfile {
  candidate_id: number;
  parsed_json: Record<string, any>;
  target_roles: string[] | null;
  target_industries: string[] | null;
  created_at: string | null;
}
