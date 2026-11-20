# ADR 0001: Use a monorepo

- Status: Accepted
- Date: 2026-11-18

## Context

The product will contain several services, infrastructure, tests, and a dashboard that evolve
together.

## Decision

Keep all components in one repository, with an independent project per service.

## Alternatives

Separate repositories give teams independent permissions and release histories but add version
coordination and duplicated automation.

## Consequences

Cross-service changes and local setup stay visible in one pull request. CI must avoid rebuilding
unaffected components as the repository grows.
