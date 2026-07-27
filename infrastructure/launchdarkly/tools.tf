# AI tool resources: one explicit launchdarkly_ai_tool per tool, defined directly
# here (no local.tools list, no for_each mapping). schema_json and custom_parameters
# are jsonencode(...) of inline objects, matching the previous mapped output.

resource "launchdarkly_ai_tool" "query_snowflake_policy_data" {
  project_key = var.project_key
  key         = "query_snowflake_policy_data"
  description = "Queries the Snowflake data warehouse for policy coverage details, benefits, and claim history. Returns structured policy information including deductibles, copays, coinsurance, and coverage limits."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "policy_id" = {
        "type"        = "string"
        "description" = "The unique policy identifier (e.g., TH-HMO-GOLD-2024)"
      }
      "query_type" = {
        "type" = "string"
        "enum" = [
          "coverage_details",
          "deductible_status",
          "claim_history",
          "benefit_summary"
        ]
        "description" = "Type of policy information to retrieve"
      }
      "coverage_category" = {
        "type"        = "string"
        "description" = "Optional: Specific coverage category to query (e.g., 'prescription', 'mental_health', 'preventive_care')"
      }
    }
    "required" = [
      "policy_id",
      "query_type"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Query Snowflake Policy Database"
    category = "database"
    returns = {
      "type"        = "object"
      "description" = "Policy data including coverage limits, copays, deductibles, and benefits"
    }
  })
}

resource "launchdarkly_ai_tool" "query_aws_rds_coverage" {
  project_key = var.project_key
  key         = "query_aws_rds_coverage"
  description = "Retrieves real-time coverage and authorization data from AWS RDS PostgreSQL database. Includes prior authorization requirements, referral needs, and network restrictions."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "member_id" = {
        "type"        = "string"
        "description" = "Member identification number"
      }
      "service_type" = {
        "type"        = "string"
        "description" = "Type of medical service (e.g., 'specialist_visit', 'surgery', 'imaging', 'therapy')"
      }
      "check_authorization" = {
        "type"        = "boolean"
        "description" = "Whether to check prior authorization requirements"
      }
    }
    "required" = [
      "member_id",
      "service_type"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Query AWS RDS Coverage Database"
    category = "database"
    returns = {
      "type"        = "object"
      "description" = "Coverage status, authorization requirements, and network information"
    }
  })
}

resource "launchdarkly_ai_tool" "calculate_out_of_pocket_costs" {
  project_key = var.project_key
  key         = "calculate_out_of_pocket_costs"
  description = "Calculates patient's estimated out-of-pocket costs based on deductible progress, coinsurance, copays, and out-of-pocket maximum. Performs real-time cost estimation for proposed services."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "policy_id" = {
        "type"        = "string"
        "description" = "Policy identifier"
      }
      "service_code" = {
        "type"        = "string"
        "description" = "CPT or service code for the medical procedure"
      }
      "estimated_charge" = {
        "type"        = "number"
        "description" = "Estimated charge amount in dollars"
      }
      "in_network" = {
        "type"        = "boolean"
        "description" = "Whether the provider is in-network"
      }
    }
    "required" = [
      "policy_id",
      "service_code",
      "estimated_charge",
      "in_network"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Calculate Out-of-Pocket Costs"
    category = "calculation"
    returns = {
      "type"        = "object"
      "description" = "Estimated patient responsibility including copay, coinsurance, and deductible portions"
    }
  })
}

resource "launchdarkly_ai_tool" "query_athena_claims_history" {
  project_key = var.project_key
  key         = "query_athena_claims_history"
  description = "Queries AWS Athena for historical claims data and spending patterns. Analyzes deductible progress, spending trends, and utilization patterns stored in S3 data lake."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "member_id" = {
        "type"        = "string"
        "description" = "Member identification number"
      }
      "date_range" = {
        "type" = "object"
        "properties" = {
          "start_date" = {
            "type"        = "string"
            "format"      = "date"
            "description" = "Start date for claims query (YYYY-MM-DD)"
          }
          "end_date" = {
            "type"        = "string"
            "format"      = "date"
            "description" = "End date for claims query (YYYY-MM-DD)"
          }
        }
        "required" = [
          "start_date",
          "end_date"
        ]
      }
      "aggregate_by" = {
        "type" = "string"
        "enum" = [
          "provider",
          "service_type",
          "month",
          "none"
        ]
        "description" = "How to aggregate claims data"
      }
    }
    "required" = [
      "member_id",
      "date_range"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Query Athena Claims Analytics"
    category = "analytics"
    returns = {
      "type"        = "object"
      "description" = "Claims history with spending totals, deductible progress, and utilization metrics"
    }
  })
}

resource "launchdarkly_ai_tool" "verify_prior_authorization" {
  project_key = var.project_key
  key         = "verify_prior_authorization"
  description = "Checks whether a specific medical service requires prior authorization and retrieves authorization status if already submitted. Integrates with payer authorization systems."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "policy_id" = {
        "type"        = "string"
        "description" = "Policy identifier"
      }
      "procedure_code" = {
        "type"        = "string"
        "description" = "CPT or HCPCS procedure code"
      }
      "provider_npi" = {
        "type"        = "string"
        "description" = "National Provider Identifier (NPI) number"
      }
      "check_existing_auth" = {
        "type"        = "boolean"
        "description" = "Check if authorization already exists"
      }
    }
    "required" = [
      "policy_id",
      "procedure_code"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Verify Prior Authorization Requirements"
    category = "verification"
    returns = {
      "type"        = "object"
      "description" = "Authorization requirements, status, and reference numbers if applicable"
    }
  })
}

resource "launchdarkly_ai_tool" "search_snowflake_provider_directory" {
  project_key = var.project_key
  key         = "search_snowflake_provider_directory"
  description = "Searches comprehensive provider directory in Snowflake data warehouse. Returns providers matching specialty, location, network, and availability criteria with detailed profiles."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "specialty" = {
        "type"        = "string"
        "description" = "Medical specialty or provider type (e.g., 'cardiology', 'orthopedics', 'primary care')"
      }
      "location" = {
        "type" = "object"
        "properties" = {
          "zip_code" = {
            "type"        = "string"
            "description" = "ZIP code for search center"
          }
          "radius_miles" = {
            "type"        = "number"
            "description" = "Search radius in miles"
          }
        }
        "required" = [
          "zip_code"
        ]
      }
      "network_id" = {
        "type"        = "string"
        "description" = "Insurance network identifier (e.g., 'HMO Network', 'PPO Network')"
      }
      "accepting_new_patients" = {
        "type"        = "boolean"
        "description" = "Filter for providers accepting new patients"
      }
      "languages" = {
        "type" = "array"
        "items" = {
          "type" = "string"
        }
        "description" = "Preferred languages spoken by provider"
      }
    }
    "required" = [
      "specialty",
      "location"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Search Snowflake Provider Directory"
    category = "database"
    returns = {
      "type"        = "array"
      "description" = "List of matching providers with details including name, address, phone, ratings, and availability"
    }
  })
}

resource "launchdarkly_ai_tool" "check_google_calendar_availability" {
  project_key = var.project_key
  key         = "check_google_calendar_availability"
  description = "Queries provider availability through Google Calendar API integration. Returns available appointment slots based on provider schedules, existing appointments, and booking rules."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "provider_id" = {
        "type"        = "string"
        "description" = "Provider's unique identifier"
      }
      "calendar_id" = {
        "type"        = "string"
        "description" = "Google Calendar ID for the provider"
      }
      "date_range" = {
        "type" = "object"
        "properties" = {
          "start_date" = {
            "type"        = "string"
            "format"      = "date"
            "description" = "Start date for availability search (YYYY-MM-DD)"
          }
          "end_date" = {
            "type"        = "string"
            "format"      = "date"
            "description" = "End date for availability search (YYYY-MM-DD)"
          }
        }
        "required" = [
          "start_date",
          "end_date"
        ]
      }
      "appointment_type" = {
        "type" = "string"
        "enum" = [
          "new_patient",
          "follow_up",
          "annual_physical",
          "consultation",
          "procedure"
        ]
        "description" = "Type of appointment to schedule"
      }
      "duration_minutes" = {
        "type"        = "number"
        "description" = "Required appointment duration in minutes"
      }
    }
    "required" = [
      "provider_id",
      "calendar_id",
      "date_range",
      "duration_minutes"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Check Provider Calendar Availability (Google Calendar MCP)"
    category = "calendar"
    returns = {
      "type"        = "array"
      "description" = "List of available time slots with start time, end time, and slot ID"
    }
  })
}

resource "launchdarkly_ai_tool" "book_calendly_appointment" {
  project_key = var.project_key
  key         = "book_calendly_appointment"
  description = "Creates appointment booking through Calendly API integration. Handles scheduling, confirmation emails, calendar invites, and appointment reminders."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "event_type_uuid" = {
        "type"        = "string"
        "description" = "Calendly event type UUID (identifies appointment type and provider)"
      }
      "start_time" = {
        "type"        = "string"
        "format"      = "date-time"
        "description" = "Appointment start time in ISO 8601 format"
      }
      "invitee_email" = {
        "type"        = "string"
        "format"      = "email"
        "description" = "Patient email address"
      }
      "invitee_name" = {
        "type"        = "string"
        "description" = "Patient full name"
      }
      "invitee_phone" = {
        "type"        = "string"
        "description" = "Patient phone number"
      }
      "custom_questions" = {
        "type" = "array"
        "items" = {
          "type" = "object"
          "properties" = {
            "question" = {
              "type" = "string"
            }
            "answer" = {
              "type" = "string"
            }
          }
        }
        "description" = "Responses to custom intake questions"
      }
      "timezone" = {
        "type"        = "string"
        "description" = "Patient timezone (e.g., 'America/Los_Angeles')"
      }
    }
    "required" = [
      "event_type_uuid",
      "start_time",
      "invitee_email",
      "invitee_name",
      "timezone"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Book Appointment via Calendly MCP"
    category = "calendar"
    returns = {
      "type"        = "object"
      "description" = "Booking confirmation with appointment details, booking ID, and calendar invite"
    }
  })
}

resource "launchdarkly_ai_tool" "send_twilio_appointment_reminder" {
  project_key = var.project_key
  key         = "send_twilio_appointment_reminder"
  description = "Sends SMS or voice appointment reminders through Twilio API. Supports customizable reminder templates, timing, and confirmation requests."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "to_phone" = {
        "type"        = "string"
        "description" = "Patient phone number in E.164 format (e.g., +14155551234)"
      }
      "appointment_details" = {
        "type" = "object"
        "properties" = {
          "appointment_id" = {
            "type" = "string"
          }
          "provider_name" = {
            "type" = "string"
          }
          "appointment_time" = {
            "type"   = "string"
            "format" = "date-time"
          }
          "location" = {
            "type" = "string"
          }
        }
        "required" = [
          "appointment_id",
          "provider_name",
          "appointment_time"
        ]
      }
      "reminder_type" = {
        "type" = "string"
        "enum" = [
          "sms",
          "voice",
          "both"
        ]
        "description" = "Type of reminder to send"
      }
      "hours_before" = {
        "type"        = "number"
        "description" = "Hours before appointment to send reminder (e.g., 24, 48)"
      }
      "include_confirm_link" = {
        "type"        = "boolean"
        "description" = "Include link for appointment confirmation"
      }
    }
    "required" = [
      "to_phone",
      "appointment_details",
      "reminder_type",
      "hours_before"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Send Appointment Reminder via Twilio"
    category = "communication"
    returns = {
      "type"        = "object"
      "description" = "Message delivery status with SID and delivery timestamps"
    }
  })
}

resource "launchdarkly_ai_tool" "get_provider_ratings_reviews" {
  project_key = var.project_key
  key         = "get_provider_ratings_reviews"
  description = "Retrieves aggregated provider ratings, patient reviews, and quality metrics from internal database and external sources (Healthgrades, Vitals, etc.)."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "provider_npi" = {
        "type"        = "string"
        "description" = "National Provider Identifier (NPI)"
      }
      "include_external_sources" = {
        "type"        = "boolean"
        "description" = "Include ratings from external review sites"
      }
      "min_review_count" = {
        "type"        = "number"
        "description" = "Minimum number of reviews required to return provider"
      }
    }
    "required" = [
      "provider_npi"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Get Provider Ratings and Reviews"
    category = "data_retrieval"
    returns = {
      "type"        = "object"
      "description" = "Provider ratings, review summaries, quality scores, and patient satisfaction metrics"
    }
  })
}

resource "launchdarkly_ai_tool" "verify_network_status" {
  project_key = var.project_key
  key         = "verify_network_status"
  description = "Real-time verification of provider's network participation status. Checks current network status, effective dates, and credential validity."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "provider_npi" = {
        "type"        = "string"
        "description" = "National Provider Identifier (NPI)"
      }
      "network_id" = {
        "type"        = "string"
        "description" = "Insurance network identifier"
      }
      "policy_id" = {
        "type"        = "string"
        "description" = "Patient policy ID for network verification"
      }
      "service_date" = {
        "type"        = "string"
        "format"      = "date"
        "description" = "Date of service for network status check"
      }
    }
    "required" = [
      "provider_npi",
      "network_id"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Verify Provider Network Status"
    category = "verification"
    returns = {
      "type"        = "object"
      "description" = "Network status, participation dates, and credential verification results"
    }
  })
}

resource "launchdarkly_ai_tool" "classify_query_intent_aws_comprehend" {
  project_key = var.project_key
  key         = "classify_query_intent_aws_comprehend"
  description = "Uses AWS Comprehend NLP to classify customer query intent and extract key entities. Analyzes sentiment, extracts medical terms, and identifies routing needs."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "query_text" = {
        "type"        = "string"
        "description" = "Customer's natural language query"
      }
      "extract_entities" = {
        "type"        = "boolean"
        "description" = "Extract named entities (medical terms, locations, provider names)"
      }
      "detect_sentiment" = {
        "type"        = "boolean"
        "description" = "Analyze sentiment (positive, negative, neutral, mixed)"
      }
      "detect_pii" = {
        "type"        = "boolean"
        "description" = "Detect personally identifiable information"
      }
    }
    "required" = [
      "query_text"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Classify Query Intent (AWS Comprehend)"
    category = "nlp"
    returns = {
      "type"        = "object"
      "description" = "Intent classification, entities, sentiment, and confidence scores"
    }
  })
}

resource "launchdarkly_ai_tool" "assess_urgency_level" {
  project_key = var.project_key
  key         = "assess_urgency_level"
  description = "Analyzes query urgency using keyword matching and ML classification. Identifies emergency situations, time-sensitive requests, and priority levels for appropriate routing."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "query_text" = {
        "type"        = "string"
        "description" = "Customer query to assess"
      }
      "user_context" = {
        "type"        = "object"
        "description" = "Additional context about user (policy status, recent claims, etc.)"
      }
    }
    "required" = [
      "query_text"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Assess Query Urgency Level"
    category = "classification"
    returns = {
      "type"        = "object"
      "description" = "Urgency level (emergency, high, medium, low), reasoning, and recommended action"
    }
  })
}

resource "launchdarkly_ai_tool" "translate_text_aws_translate" {
  project_key = var.project_key
  key         = "translate_text_aws_translate"
  description = "Translates customer queries and agent responses using AWS Translate. Supports 75+ languages with medical terminology preservation."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "text" = {
        "type"        = "string"
        "description" = "Text to translate"
      }
      "source_language" = {
        "type"        = "string"
        "description" = "Source language code (e.g., 'en', 'es', 'zh'). Use 'auto' for automatic detection."
      }
      "target_language" = {
        "type"        = "string"
        "description" = "Target language code"
      }
      "preserve_medical_terms" = {
        "type"        = "boolean"
        "description" = "Use medical terminology glossary for accurate translation"
      }
    }
    "required" = [
      "text",
      "target_language"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Translate Text (AWS Translate)"
    category = "nlp"
    returns = {
      "type"        = "object"
      "description" = "Translated text with detected source language and confidence score"
    }
  })
}

resource "launchdarkly_ai_tool" "analyze_sentiment_aws_comprehend" {
  project_key = var.project_key
  key         = "analyze_sentiment_aws_comprehend"
  description = "Analyzes sentiment and emotional tone of customer messages using AWS Comprehend. Helps brand voice agent adjust response tone appropriately."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "text" = {
        "type"        = "string"
        "description" = "Text to analyze"
      }
      "language_code" = {
        "type"        = "string"
        "description" = "Language code (e.g., 'en', 'es')"
      }
    }
    "required" = [
      "text",
      "language_code"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Analyze Sentiment (AWS Comprehend)"
    category = "nlp"
    returns = {
      "type"        = "object"
      "description" = "Sentiment (positive, negative, neutral, mixed) with confidence scores for each"
    }
  })
}

resource "launchdarkly_ai_tool" "check_accessibility_requirements" {
  project_key = var.project_key
  key         = "check_accessibility_requirements"
  description = "Analyzes user profile for accessibility needs (screen reader compatibility, large text, high contrast) and formats response accordingly. Ensures WCAG 2.1 AA compliance."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "user_id" = {
        "type"        = "string"
        "description" = "User identifier"
      }
      "response_text" = {
        "type"        = "string"
        "description" = "Response text to format"
      }
      "check_readability" = {
        "type"        = "boolean"
        "description" = "Check Flesch-Kincaid readability score"
      }
    }
    "required" = [
      "user_id",
      "response_text"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Check Accessibility Requirements"
    category = "formatting"
    returns = {
      "type"        = "object"
      "description" = "Accessibility requirements, formatted response, and WCAG compliance status"
    }
  })
}

resource "launchdarkly_ai_tool" "apply_brand_guidelines" {
  project_key = var.project_key
  key         = "apply_brand_guidelines"
  description = "Validates and adjusts response text to match brand voice guidelines. Checks tone, terminology, formatting, and compliance with brand standards from style guide database."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "response_text" = {
        "type"        = "string"
        "description" = "Response text to validate and adjust"
      }
      "target_tone" = {
        "type" = "string"
        "enum" = [
          "empathetic",
          "professional",
          "friendly",
          "urgent",
          "educational"
        ]
        "description" = "Target tone for the response"
      }
      "validate_compliance" = {
        "type"        = "boolean"
        "description" = "Check for HIPAA and regulatory compliance"
      }
      "max_reading_level" = {
        "type"        = "number"
        "description" = "Maximum reading grade level (e.g., 8.0 for 8th grade)"
      }
    }
    "required" = [
      "response_text",
      "target_tone"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Apply Brand Voice Guidelines"
    category = "formatting"
    returns = {
      "type"        = "object"
      "description" = "Adjusted response text, compliance status, readability score, and brand guideline adherence"
    }
  })
}

resource "launchdarkly_ai_tool" "get_provider_schedule_details" {
  project_key = var.project_key
  key         = "get_provider_schedule_details"
  description = "Retrieves detailed provider scheduling information including working hours, break times, appointment types offered, booking windows, and cancellation policies."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "provider_id" = {
        "type"        = "string"
        "description" = "Provider unique identifier"
      }
      "location_id" = {
        "type"        = "string"
        "description" = "Specific practice location ID"
      }
      "date" = {
        "type"        = "string"
        "format"      = "date"
        "description" = "Date to retrieve schedule for"
      }
    }
    "required" = [
      "provider_id"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Get Provider Schedule and Booking Rules"
    category = "data_retrieval"
    returns = {
      "type"        = "object"
      "description" = "Schedule details, working hours, appointment types, and booking rules"
    }
  })
}

resource "launchdarkly_ai_tool" "cancel_or_reschedule_appointment" {
  project_key = var.project_key
  key         = "cancel_or_reschedule_appointment"
  description = "Handles appointment cancellations and rescheduling through integrated scheduling system. Updates calendars, sends notifications, and manages waitlists."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "appointment_id" = {
        "type"        = "string"
        "description" = "Unique appointment identifier"
      }
      "action" = {
        "type" = "string"
        "enum" = [
          "cancel",
          "reschedule"
        ]
        "description" = "Action to perform"
      }
      "new_start_time" = {
        "type"        = "string"
        "format"      = "date-time"
        "description" = "New appointment time (required for reschedule)"
      }
      "cancellation_reason" = {
        "type"        = "string"
        "description" = "Reason for cancellation (optional)"
      }
      "notify_patient" = {
        "type"        = "boolean"
        "description" = "Send confirmation notification to patient"
      }
      "add_to_waitlist" = {
        "type"        = "boolean"
        "description" = "Add patient to waitlist for earlier availability"
      }
    }
    "required" = [
      "appointment_id",
      "action"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Cancel or Reschedule Appointment"
    category = "calendar"
    returns = {
      "type"        = "object"
      "description" = "Confirmation of cancellation/reschedule with updated appointment details"
    }
  })
}

resource "launchdarkly_ai_tool" "search_medical_knowledge_base" {
  project_key = var.project_key
  key         = "search_medical_knowledge_base"
  description = "Searches internal medical knowledge base and clinical guidelines for procedure information, coverage criteria, and medical necessity guidelines."
  schema_json = jsonencode({
    "type" = "object"
    "properties" = {
      "query" = {
        "type"        = "string"
        "description" = "Search query"
      }
      "search_type" = {
        "type" = "string"
        "enum" = [
          "procedure_code",
          "diagnosis_code",
          "clinical_guideline",
          "coverage_policy"
        ]
        "description" = "Type of information to search for"
      }
      "filter_by_policy_type" = {
        "type"        = "string"
        "description" = "Filter results by policy type (HMO, PPO, EPO)"
      }
    }
    "required" = [
      "query",
      "search_type"
    ]
  })

  custom_parameters = jsonencode({
    name     = "Search Medical Knowledge Base"
    category = "knowledge"
    returns = {
      "type"        = "object"
      "description" = "Relevant knowledge base articles, guidelines, and coverage policies"
    }
  })
}

# Keyed map of the explicit tool resources above, consumed by outputs.tf.
locals {
  ai_tool_resources = {
    for r in [
      launchdarkly_ai_tool.query_snowflake_policy_data,
      launchdarkly_ai_tool.query_aws_rds_coverage,
      launchdarkly_ai_tool.calculate_out_of_pocket_costs,
      launchdarkly_ai_tool.query_athena_claims_history,
      launchdarkly_ai_tool.verify_prior_authorization,
      launchdarkly_ai_tool.search_snowflake_provider_directory,
      launchdarkly_ai_tool.check_google_calendar_availability,
      launchdarkly_ai_tool.book_calendly_appointment,
      launchdarkly_ai_tool.send_twilio_appointment_reminder,
      launchdarkly_ai_tool.get_provider_ratings_reviews,
      launchdarkly_ai_tool.verify_network_status,
      launchdarkly_ai_tool.classify_query_intent_aws_comprehend,
      launchdarkly_ai_tool.assess_urgency_level,
      launchdarkly_ai_tool.translate_text_aws_translate,
      launchdarkly_ai_tool.analyze_sentiment_aws_comprehend,
      launchdarkly_ai_tool.check_accessibility_requirements,
      launchdarkly_ai_tool.apply_brand_guidelines,
      launchdarkly_ai_tool.get_provider_schedule_details,
      launchdarkly_ai_tool.cancel_or_reschedule_appointment,
      launchdarkly_ai_tool.search_medical_knowledge_base,
    ] : r.key => r
  }
}

