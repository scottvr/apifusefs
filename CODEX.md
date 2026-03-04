----
title: this is a dump of a good summary of our plan for apifuse.py,
as spoken by GPT-5.3-Codex in a sidebar chat while working on this. The purpose of this doc is for us to not lose site of our imediate goals, while not having to try to keep it all in forefront context
----

Yes. The right long-term direction is to treat the OpenAPI document as the primary contract and use runtime probing only as a fallback.

A practical model is:

Use the spec first.

Build the filesystem shape from paths, response schemas, parameter definitions, and content types.
Infer “collection” vs “item” by path pairs like /users and /users/{user_id}.
Infer directory vs file shape from the response schema:
object => directory with properties as children
scalar => file
array<object> => directory of indexed entries or id-keyed entries
array<scalar> => directory of numbered files
Prefer schema-defined property names over sampling live payloads when possible.
Use live responses second.

Use actual payloads to fill in dynamic names like resource ids.
If the spec claims a collection exists but GET /users fails, fall back to item probing only when an item route exists.
If the payload shape disagrees with the schema, log it and prefer the live payload for that request.
For caching, a layered approach makes sense:

Cache parsed spec aggressively.
Cache collection listings and resource payloads with a TTL.
On expiry, if the endpoint supports validators, revalidate with If-None-Match / If-Modified-Since.
HEAD is useful only if the API actually returns meaningful ETag or Last-Modified; many don’t. In practice, conditional GET is usually more reliable than HEAD.
Make cache behavior configurable: --cache-ttl, --cache-max-entries, --cache-strategy.
For “URL only” mode, the discovery order should be deterministic:

Try /openapi.json
Try /swagger.json
Try /openapi.yaml
Try /openapi.yml
Optionally inspect /docs, /redoc, /swagger for embedded schema links
If none exist, fall back to a generic raw HTTP browser mode rather than pretending we understand the API contract
That split keeps behavior honest:

“spec mode” = structured filesystem derived from the contract
“no-spec mode” = best-effort exploratory filesystem, much less opinionated
Given the API you control now, the next high-value implementation step is a schema-aware mapper:

parse response schemas for /users/ and /users/{user_id}
decide the expected directory/file structure from the schema before the first live request
then add a TTL cache on top of the current runtime fetches
