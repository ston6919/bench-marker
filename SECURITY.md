# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's security advisory
feature instead of opening a public issue.

## Deployment guidance

Bench Marker can store an OpenRouter API key and can initiate billable model
requests. It has no built-in user authentication. By default it binds to
`127.0.0.1`; keep that default and put an authenticated, TLS-enabled reverse
proxy in front of it if remote access is required.

Never commit `.env`, `data/settings.json`, `data/bench.db`, generated work
directories, or render logs. They can contain credentials, prompts, model
outputs, scores, and usage details.
