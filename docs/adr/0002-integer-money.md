# ADR 0002: Store money as integer minor units

- Status: Accepted
- Date: 2026-11-18

## Context

Binary floating-point cannot represent many decimal currency amounts exactly.

## Decision

Represent amounts as Python `int` values and PostgreSQL `BIGINT` columns in the currency's minor
unit, such as cents. Store the currency separately.

## Alternatives

Fixed-precision `NUMERIC` supports fractional values but requires explicit scale and rounding
rules. Floating-point is unsuitable for money.

## Consequences

Arithmetic and equality are exact. API clients must understand that `1050` USD means $10.50, and
currency metadata must define minor-unit display rules.
