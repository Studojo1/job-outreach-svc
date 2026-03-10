export interface LeadScore {
  overall: number;
  title_relevance: number;
  department_relevance: number;
  industry_relevance: number;
  seniority_relevance: number;
  location_relevance: number;
  explanation: string | null;
}

export interface Lead {
  id: number;
  name: string;
  title: string;
  company: string;
  industry: string | null;
  location: string | null;
  linkedin_url: string | null;
  email: string | null;
  email_verified: boolean;
  company_size: string | null;
  status: string;
  score: LeadScore | null;
}
