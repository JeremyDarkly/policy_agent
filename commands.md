` docker compose run --rm terraform init` - very first time provisioning resources
`docker compose run --rm terraform destroy` - delete all resources
`docker compose run --rm terraform plan` - indicate the planned updates
`docker compose run --rm terraform apply` - applies the updates to the `LAUNCHDARKLY_PROJECT_KEY` defined in `.env`

`docker compose up` - runs the frontend and backend (NOTE! you must be logged into AWS via `aws sso login --profile <my_profile>`)
