# Credits, sources and third-party licenses

Lakeloom is licensed under the [Apache License 2.0](LICENSE). It builds on the work of many
people and projects. This file credits them, links to their sites, and records the license
of everything Lakeloom uses, ships or refers to.

Lakeloom is an independent project. Naming a work, project or standard here does not imply
any affiliation with, or endorsement by, its authors.

## Method and sources

| Work | Author / publisher | Use in Lakeloom |
|------|--------------------|-----------------|
| *Ontology Pipeline: A Framework for Building Knowledge Infrastructures* (book, 2026) | [Jessica Talisman](https://jessicatalisman.substack.com/about); [Technics Publications](https://technicspub.com/) | The staged method (controlled vocabulary → metadata schema/MAP → taxonomy → thesaurus → ontology → knowledge graph) that structures the pipeline tooling and the guides. *The Ontology Pipeline®* is a registered trademark of Jessica Talisman, used here only to name and credit her method. |
| [*The Ontology Pipeline®*](https://jessicatalisman.substack.com/p/the-ontology-pipeline) and [*The Ontology Pipeline™, Refresh*](https://jessicatalisman.substack.com/p/the-ontology-pipeline-refreshed) (articles) | Jessica Talisman, *Intentional Arrangement* | The public introduction to the same framework |
| [ANSI/NISO Z39.19-2005 (R2010)](https://www.niso.org/publications/ansiniso-z3919-2005-r2010), *Guidelines for the Construction, Format, and Management of Monolingual Controlled Vocabularies* | [NISO](https://www.niso.org/) | The relationship types (BT/NT/RT, USE/UF) and the checks of `validate_thesaurus` |
| [ISO 25964-1:2011](https://www.iso.org/standard/53657.html) and [ISO 25964-2:2013](https://www.iso.org/standard/53658.html), *Thesauri and interoperability with other vocabularies* | [ISO](https://www.iso.org/) | Thesaurus structure and mappings |

Beyond four short, attributed quotations in
[`docs/ontology-pipeline-guide.md`](docs/ontology-pipeline-guide.md), Lakeloom reproduces no
text from the book. Its summaries are Lakeloom's own words.

## Standards

| Standard | Publisher | Use |
|----------|-----------|-----|
| [RDF 1.1](https://www.w3.org/TR/rdf11-concepts/), [Turtle](https://www.w3.org/TR/turtle/), [N-Triples](https://www.w3.org/TR/n-triples/), [RDF/XML](https://www.w3.org/TR/rdf-syntax-grammar/) | [W3C](https://www.w3.org/) | Model exports |
| [SKOS](https://www.w3.org/TR/skos-reference/) | W3C | Vocabulary, taxonomy and thesaurus representation, and the SKOS export |
| [OWL 2](https://www.w3.org/TR/owl2-overview/) | W3C | Native ontology (classes, properties, axioms), `owl:deprecated` |
| [SHACL](https://www.w3.org/TR/shacl/) | W3C | Generated shapes |
| [JSON-LD 1.1](https://www.w3.org/TR/json-ld11/) | W3C | JSON-LD context and exports |
| [SPARQL 1.1](https://www.w3.org/TR/sparql11-query/), [Graph Store Protocol](https://www.w3.org/TR/sparql11-http-rdf-update/) | W3C | `sparql_query`, triple-store sink |
| [DCMI Metadata Terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/) | [DCMI](https://www.dublincore.org/) | `dct:isReplacedBy` on deprecated concepts |
| [Model Context Protocol](https://modelcontextprotocol.io/) | Model Context Protocol, a Series of LF Projects, LLC | The agent interface |
| [OpenID Connect](https://openid.net/connect/), [OAuth 2.0 Token Exchange (RFC 8693)](https://www.rfc-editor.org/rfc/rfc8693) | OpenID Foundation, IETF | Authentication, reviewer identity |
| [OpenTelemetry](https://opentelemetry.io/) | CNCF | Traces and metrics |
| [Semantic Versioning 2.0.0](https://semver.org/) | Tom Preston-Werner | Release versions |
| [Keep a Changelog](https://keepachangelog.com/) | Olivier Lacan | `CHANGELOG.md` format |

## External services queried at run time

Lakeloom sends no data to these services. It only reads public data from them, when a user
calls `suggest_external_mappings`.

| Service | Terms of the data | Use |
|---------|-------------------|-----|
| [Wikidata](https://www.wikidata.org/) (Wikimedia Foundation) | Structured data under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | Candidate `skos:*Match` mappings |
| [schema.org](https://schema.org/) | [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) | Candidate mappings to schema.org types. The vocabulary is downloaded, cached in memory and never redistributed. Only schema.org IRIs are written to a model |

## Software Lakeloom ships

The Python package and the container image include these components unmodified. Each keeps
its own license.

### Runtime dependencies

| Package | Authors | License |
|---------|---------|---------|
| [aiosqlite](https://aiosqlite.omnilib.dev) | Amethyst Reese | MIT |
| [Alembic](https://alembic.sqlalchemy.org) | Mike Bayer | MIT |
| [asyncpg](https://github.com/MagicStack/asyncpg) | MagicStack Inc. | Apache-2.0 |
| [FastAPI](https://fastapi.tiangolo.com/) | Sebastián Ramírez | MIT |
| [HTTPX](https://www.python-httpx.org/) and [httpx2](https://github.com/pydantic/httpx2) | Tom Christie; Pydantic | BSD-3-Clause |
| [JMESPath](https://github.com/jmespath/jmespath.py) | James Saryerwinnie | MIT |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | Model Context Protocol, a Series of LF Projects, LLC | MIT |
| [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) | The OpenTelemetry Authors | Apache-2.0 |
| [Prometheus client_python](https://github.com/prometheus/client_python) | The Prometheus Authors | Apache-2.0 AND BSD-2-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) and [pydantic-settings](https://github.com/pydantic/pydantic-settings) | Samuel Colvin and contributors | MIT |
| [PyJWT](https://github.com/jpadilla/pyjwt) | José Padilla | MIT |
| [cryptography](https://cryptography.io/) | The Python Cryptographic Authority | Apache-2.0 OR BSD-3-Clause |
| [PyYAML](https://pyyaml.org/) | Kirill Simonov | MIT |
| [RDFLib](https://github.com/RDFLib/rdflib) | Daniel Krech and the RDFLib team | BSD-3-Clause |
| [SQLAlchemy](https://www.sqlalchemy.org) | Mike Bayer | MIT |
| [Typer](https://typer.tiangolo.com/) | Sebastián Ramírez | MIT |
| [Uvicorn](https://www.uvicorn.org/) | Tom Christie | BSD-3-Clause |
| [certifi](https://github.com/certifi/python-certifi) (transitive) | Kenneth Reitz | MPL-2.0 (shipped unmodified; source at the link) |

### Optional connectors (extras)

| Package | Authors | License |
|---------|---------|---------|
| [delta-rs / deltalake](https://github.com/delta-io/delta-rs) (`delta`) | The Delta Lake project | Apache-2.0 |
| [Apache Arrow / pyarrow](https://arrow.apache.org/) (`delta`) | The Apache Software Foundation | Apache-2.0 |
| [Databricks SDK for Python](https://github.com/databricks/databricks-sdk-py) (`databricks`) | Databricks, Inc. | Apache-2.0 |
| [Snowflake Connector for Python](https://github.com/snowflakedb/snowflake-connector-python) (`snowflake`) | Snowflake Inc. | Apache-2.0 |

The full transitive set (about 120 packages) is pinned in [`uv.lock`](uv.lock). All of
them are under MIT, BSD, Apache-2.0, ISC, PSF, MPL-2.0 or similar permissive licenses. None
is under GPL, LGPL or AGPL. To list them:
`uv run pip-licenses` (with [pip-licenses](https://github.com/raimon49/pip-licenses)), or
read each package's metadata.

### Container image

The image is built on [`python:3.12-slim`](https://hub.docker.com/_/python) (Debian, by the
Docker Official Images team) and installs [Git](https://git-scm.com/) and
[OpenSSH](https://www.openssh.com/) from Debian:
- Python is under the [PSF License](https://docs.python.org/3/license.html);
- Git is under [GPL-2.0](https://git-scm.com/about/free-and-open-source), and Lakeloom only
  runs it as a separate program;
- OpenSSH is under a BSD-style license;
- the Debian base contains packages under their own licenses, including GPL and LGPL.

These programs are aggregated in the image, not linked with Lakeloom, so they do not change
Lakeloom's license. Their sources are available from [Debian](https://sources.debian.org/).
The image also contains [uv](https://github.com/astral-sh/uv) (Astral Software Inc., MIT OR
Apache-2.0) at build time only.

### Bundled data files

| File | Origin | License |
|------|--------|---------|
| [`src/lakeloom/om/openapi/openmetadata-2.1.0-rc1.json`](src/lakeloom/om/openapi/openmetadata-2.1.0-rc1.json) | The OpenAPI specification of [OpenMetadata](https://github.com/open-metadata/OpenMetadata) 2.1.0-rc1, fetched from a running server by `scripts/fetch_om_openapi.sh` and key-sorted | Apache-2.0, see [NOTICE](NOTICE) |

## Software Lakeloom works with (not shipped)

| Project | Authors | License | Role |
|---------|---------|---------|------|
| [OpenMetadata](https://open-metadata.org/) | The OpenMetadata project and Collate, Inc. | Apache-2.0 | The ontology engine (required) |
| [Keycloak](https://www.keycloak.org/) | The Keycloak project (CNCF) | Apache-2.0 | Identity provider of the dev stack |
| [Gitea](https://about.gitea.com/) | The Gitea authors | MIT | Git forge of the dev stack |
| [Unity Catalog](https://www.unitycatalog.io/) | LF AI & Data Foundation | Apache-2.0 | Lake source of the dev stack |
| [Apache Jena Fuseki](https://jena.apache.org/documentation/fuseki2/) | The Apache Software Foundation | Apache-2.0 | Optional triple store of the dev stack (built from OpenMetadata's `docker/rdf-store`) |
| [PostgreSQL](https://www.postgresql.org/) | The PostgreSQL Global Development Group | PostgreSQL License | State store, OpenMetadata database |
| [Elasticsearch](https://www.elastic.co/elasticsearch) | Elasticsearch B.V. | AGPL-3.0, SSPL-1.0 or Elastic License 2.0 (your choice) | OpenMetadata's search index in the dev stack. The compose file only pulls the official image and does not redistribute it. For production, follow OpenMetadata's guidance, which also supports OpenSearch (Apache-2.0) |
| [Alpine Linux](https://alpinelinux.org/) | Alpine Linux Development Team | Various (MIT base) | Helper container of the dev stack |
| [Apache Fuseki](https://jena.apache.org/), [GraphDB](https://graphdb.ontotext.com/), [Eclipse RDF4J](https://rdf4j.org/) | ASF; Ontotext; Eclipse Foundation | Apache-2.0; proprietary (free edition); BSD-3-Clause | Supported triple stores |

## Development tools (not shipped)

| Tool | Authors | License |
|------|---------|---------|
| [uv](https://github.com/astral-sh/uv) and [Ruff](https://github.com/astral-sh/ruff) | Astral Software Inc. | MIT OR Apache-2.0 / MIT |
| [Pyright](https://github.com/microsoft/pyright) (via [pyright-python](https://github.com/RobertCraigie/pyright-python)) | Microsoft; Robert Craigie | MIT |
| [pytest](https://pytest.org/), [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio), [pytest-cov](https://github.com/pytest-dev/pytest-cov) | Holger Krekel and the pytest-dev team | MIT; Apache-2.0; MIT |
| [Hypothesis](https://hypothesis.works/) | David R. MacIver, Zac Hatfield-Dodds and contributors | MPL-2.0 |
| [RESPX](https://lundberg.github.io/respx/) | Jonas Lundberg | BSD-3-Clause |
| [pySHACL](https://github.com/RDFLib/pySHACL) and [OWL-RL](https://github.com/RDFLib/OWL-RL) | Ashley Sommer, Nicholas Car (RDFLib) | Apache-2.0; W3C Software License |
| [pip-audit](https://github.com/pypa/pip-audit) | Alex Cameron (Trail of Bits), PyPA | Apache-2.0 |

## License compatibility

- Every component Lakeloom ships is under a license that allows redistribution inside an
  Apache-2.0 project.
- The MPL-2.0 package (`certifi`) is used unmodified as a separate file set. MPL-2.0 applies
  per file and does not extend to Lakeloom.
- No shipped component is under a copyleft license that would require Lakeloom's own code to
  be released under that license.
- GPL programs in the container image (Git, parts of Debian) are separate programs, not
  libraries Lakeloom links.
- Lakeloom therefore stays under Apache-2.0, a permissive license. It lets anyone use,
  modify and redistribute the code, commercially or not, with an explicit patent grant. It
  asks only that the license and the [NOTICE](NOTICE) are kept.

## Trademarks

OpenMetadata, Collate, Databricks, Delta Lake, Unity Catalog, Snowflake, Elasticsearch,
Keycloak, Gitea, GraphDB, Claude and other names are trademarks of their respective owners.
*The Ontology Pipeline®* is a registered trademark of Jessica Talisman. They are used here
only to identify the projects and works.
