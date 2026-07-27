terraform {
  required_version = ">= 1.0"

  required_providers {
    launchdarkly = {
      source  = "launchdarkly/launchdarkly"
      version = "~> 3.0"
    }
  }
}
