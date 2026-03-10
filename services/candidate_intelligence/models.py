"""
CandidateProfiler — Pydantic Models
All data structures for resume parsing, profiling, and payload generation.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Any
from datetime import datetime
import uuid


# --- Resume Parsing Models ---

class Education(BaseModel):
    degree: str = Field("", description="Degree name, e.g. B.Tech, MBA")
    field: Optional[str] = Field(None, description="Field of study, e.g. Computer Science")
    institution: str = Field("", description="University/college name")
    year: Optional[Any] = Field(None, description="Graduation year")

    class Config:
        extra = "allow"

class Experience(BaseModel):
    title: str = Field("", description="Job title / role")
    company: str = Field("", description="Company or organization name")
    duration: str = Field("", description="Duration, e.g. '6 months', 'Jan 2023 - Jun 2023'")
    description: str = Field("", description="Brief description of responsibilities/achievements")

    class Config:
        extra = "allow"

class ResumeSummary(BaseModel):
    """Structured summary extracted from a resume via LLM."""
    name: Optional[str] = Field(None, description="Candidate's full name")
    email: Optional[str] = Field(None, description="Email address")
    phone: Optional[str] = Field(None, description="Phone number")
    education: List[Education] = Field(default_factory=list)
    experience: List[Experience] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list, description="Technical and soft skills")
    key_strengths: List[str] = Field(default_factory=list, description="Top 3-5 standout strengths")
    career_interests: List[str] = Field(default_factory=list, description="Expressed career interests or goals")
    summary_text: str = Field("", description="2-3 sentence overview of the candidate")


# --- Chat / Agent Models ---

class MCQOption(BaseModel):
    label: str = Field(..., description="Option label: A, B, C, D, E, etc.")
    text: str = Field(..., description="The option text")

class MCQQuestion(BaseModel):
    question: str = Field(..., description="The MCQ question text")
    options: List[MCQOption] = Field(..., description="List of answer options")
    allow_multiple: bool = Field(False, description="Whether user can select multiple options")

class AgentResponse(BaseModel):
    """What the profiling agent says/does at each turn."""
    message: str = Field(..., description="The text message to display to the candidate")
    current_state: Literal[
        "GREETING",
        "RESUME_SUMMARY",
        "MCQ",
        "DIAGNOSIS",
        "COUNSELING",
        "CONSENSUS",
        "PAYLOAD_READY"
    ] = Field(..., description="Current conversation state")
    mcq: Optional[MCQQuestion] = Field(None, description="MCQ question if current_state is MCQ")
    text_input: bool = Field(False, description="If true, show text input boxes instead of MCQ (e.g. for salary min/max)")
    is_complete: bool = Field(False, description="Whether the profiling session is complete")
    questions_asked_so_far: int = Field(0, description="Counter of questions asked")


# --- Final Payload Models (tolerant of LLM output variations) ---

class SalaryRange(BaseModel):
    min_annual_ctc: Any = Field(0, description="Minimum expected annual CTC")
    max_annual_ctc: Any = Field(0, description="Maximum expected annual CTC")
    currency: str = Field("INR", description="Currency code")

    class Config:
        extra = "allow"

class PersonalInfo(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    education: Any = Field(default_factory=list)
    skills_detected: List[str] = Field(default_factory=list)

    class Config:
        extra = "allow"

class CandidatePreferences(BaseModel):
    locations: List[str] = Field(default_factory=list, description="Preferred job locations")
    work_mode: Optional[str] = Field("flexible", description="Work mode preference")
    company_size: Optional[str] = Field("any", description="Preferred company size")
    company_stage: Optional[str] = Field("any", description="Company stage preference")
    industry_interests: List[str] = Field(default_factory=list, description="Industries of interest")
    salary_expectations: Optional[Any] = Field(default_factory=lambda: {"min_annual_ctc": 0, "max_annual_ctc": 0, "currency": "INR"})
    risk_tolerance: Optional[str] = Field("medium", description="Startup risk tolerance")
    timeline: Optional[str] = Field("flexible", description="Job search timeline")

    class Config:
        extra = "allow"

class SpecializationFit(BaseModel):
    name: str = Field("", description="Specialization name within the career cluster")
    fit_score: float = Field(0.5, ge=0, le=1, description="How well the candidate fits (0-1)")
    reasoning: str = Field("", description="Why this specialization fits")

    class Config:
        extra = "allow"

class RoleFit(BaseModel):
    title: str = Field("", description="Specific job role title")
    seniority: str = Field("entry")
    fit_score: float = Field(0.5, ge=0, le=1, description="How well candidate fits (0-1)")
    salary_alignment: Any = Field(True, description="Whether role salary aligns with expectations")
    reasoning: str = Field("", description="Why this role is recommended")

    class Config:
        extra = "allow"

class CareerAnalysis(BaseModel):
    primary_cluster: str = Field("General", description="Primary career domain")
    secondary_cluster: Optional[str] = Field(None, description="Secondary career domain")
    specializations: List[SpecializationFit] = Field(default_factory=lambda: [SpecializationFit(name="General", fit_score=0.5, reasoning="Based on conversation")])
    recommended_roles: List[RoleFit] = Field(default_factory=lambda: [RoleFit(title="Associate", fit_score=0.5, reasoning="Based on conversation")])
    transition_paths: List[str] = Field(default_factory=list, description="Career transition suggestions")

    class Config:
        extra = "allow"

class SessionMetadata(BaseModel):
    resume_uploaded: bool = False
    questions_answered: int = 0
    session_duration_seconds: Optional[int] = None
    confidence_score: float = Field(0.0, ge=0, le=1)

    class Config:
        extra = "allow"

class CandidatePayload(BaseModel):
    """The final comprehensive candidate profile payload."""
    candidate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    profile_summary: str = Field("Profile generated from conversation.", description="AI-generated candidate overview")
    personal_info: Optional[PersonalInfo] = Field(default_factory=PersonalInfo)
    preferences: Optional[CandidatePreferences] = Field(default_factory=CandidatePreferences)
    career_analysis: Optional[CareerAnalysis] = Field(default_factory=CareerAnalysis)
    session_metadata: Optional[SessionMetadata] = Field(default_factory=SessionMetadata)

    class Config:
        extra = "allow"


# --- Chat History ---

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = Field(...)
    content: str = Field(...)
    mcq: Optional[MCQQuestion] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())