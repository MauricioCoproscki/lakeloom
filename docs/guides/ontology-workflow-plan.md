# Plano: Workflows de Construção de Ontologias via MCP

Objetivo: permitir que o usuário do lakeloom crie Vocabulário Controlado, MAP, Taxonomia,
Thesaurus e Ontologia tanto pela ingestão do lake (path já existente) quanto pela criação
direta via ferramentas MCP.

Baseado em: Jessica Talisman, *Ontology Pipeline: A Framework for Building Knowledge
Infrastructures* (Technics Publications, 2026; ASIN B0HJ8QWJ5V) — *The Ontology Pipeline®* é
marca registrada da autora; referências completas em [THIRD_PARTY.md](../../THIRD_PARTY.md) —
+ análise da codebase em 2026-11-18.

> **Status (2026-11-19):** plano executado em M5–M9 e revisado pela auditoria da pipeline.
> Os nomes das ferramentas neste documento já seguem o padrão `verbo_objeto` adotado na
> auditoria. O estado atual, por etapa, está na seção 2 de
> [`ontology-pipeline-guide.md`](../ontology-pipeline-guide.md).

---

## Estado Atual

O lakeloom já tem:

- **Ingestão lake→ontologia**: `scan` → `mapper.py` → `proposals.py` → OM change sets
- **Engine RDF completa**: SKOS + OWL via OM Ontology Studio + rdflib
- **MCP tools existentes**: `create_model`, `propose_from_scan`, `propose_change`, `submit_proposal`, `review_proposal`, `import_model`, `find_concepts`, `explain_concept`, `find_assets_for_concept`
- **Publicação**: SHACL (`shacl_gen.py`), JSON-LD (`jsonld_context.py`), Turtle, Git store, triple-store sink

O que falta são **workflows de construção direta** para os estágios 1-4 e melhoria do estágio 6.

---

## Princípios de Design

1. **Dois paths, mesma infra**: criação direta e ingestão do lake usam as mesmas `ProposalOperation`s — o que muda é de onde vêm as operações (usuário vs. mapper).
2. **Proposta antes de aplicar**: toda criação passa por proposal → review, igual ao path de scan. Nenhuma escrita direta no OM sem confirm token.
3. **MCP-first**: cada novo workflow expõe ferramenta MCP com `ToolAnnotations` correto; CLI e HTTP API derivam dessas ferramentas.
4. **Sem LLM obrigatório**: as ferramentas funcionam sem LLM. O agente/LLM que chama as ferramentas enriquece os inputs — as ferramentas não dependem disso.

---

## Milestone M5 — Workflows CV, MAP, Taxonomia, Thesaurus (criação direta)

### M5a — Operações de Escrita Direta (core)

**Novas operações em `core/operations.py`:**

```python
# Adicionar ao __all__ e implementar:
UpdateConcept  # já existe — verificar se cobre description + scopeNote
AddScopeNote  # nova: skos:scopeNote em conceito existente
AddHistoryNote  # nova: skos:historyNote (thesaurus)
DeprecateConcept  # nova: marca conceito como deprecated + substituto opcional
RetireConcept  # nova: remove conceito com tombstone (skos:hiddenLabel)
SetCoverageModel  # nova: metadados do modelo de cobertura no ConceptScheme
```

**Novo módulo `core/vocabulary.py`:**

```python
# Workflow de construção de CV do zero (sem ingestão de lake)
# Funções:
# - create_candidate(term, source_text, authority) → AddConcept op
# - harvest_folksonomy(model_slug) → list[AddConcept] candidatos das tags OM
# - dedupe_candidates(candidates, model) → list[Match] (reutiliza dedupe.py)
# - batch_add_terms(terms: list[TermDraft]) → Proposal
# - export_cv_report(model_slug) → markdown com estatísticas do CV
```

**Novo módulo `core/thesaurus.py`:**

```python
# Validação e construção de thesaurus formal
# Funções:
# - validate_bt_nt_cycles(graph) → list[ValidationError]  — detecta ciclos BT/NT
# - validate_rt_symmetry(graph) → list[ValidationError]   — RT deve ser simétrico
# - validate_ansi_z39(graph) → ThesaurusReport            — relatório completo
# - suggest_related(concept_fqn, model, top_k=10) → list[RelatedSuggestion]
#   (baseado em co-ocorrência de realizações, não embeddings)
```

**Novo módulo `core/taxonomy.py`:**

```python
# Modelo de cobertura para taxonomias (livro, estágio 3)
# Dataclass CoverageModel:
#   topics: list[str]         — quais tópicos cobrir
#   depth: int                — níveis máximos de hierarquia
#   granularity: str          — "coarse" | "medium" | "fine"
#   exclusions: list[str]     — tópicos explicitamente fora de escopo
# Funções:
# - assess_coverage(model_slug, coverage_model) → CoverageReport
#   (compara a taxonomia atual com o modelo de cobertura)
# - coverage_gaps(report) → list[str]  — tópicos faltantes
```

---

### M5b — Ferramentas MCP (server)

**Novo arquivo `server/tools_cv.py`** — ferramentas para CV, Taxonomia, Thesaurus:

```python
# Ferramentas READ (ToolAnnotations: read_only_hint=True):

"harvest_vocabulary_candidates"
# Lista candidatos a termos minerados automaticamente das tags OM do modelo.
# Params: model (str), limit (int)
# Returns: lista de {term, source_tag, frequency, existing_match}
# Implementação: cv_builder.harvest_folksonomy()

"assess_realization_coverage"
# Avalia a cobertura da taxonomia atual contra um modelo de cobertura definido.
# Params: model (str), topics (list[str]), depth (int), granularity (str)
# Returns: CoverageReport markdown com gaps identificados

"validate_thesaurus"
# Valida o thesaurus contra ANSI Z39.19: ciclos BT/NT, assimetria RT, missing scopeNotes.
# Params: model (str)
# Returns: ThesaurusReport com erros, avisos e estatísticas

"suggest_related_terms"
# Sugere termos RT candidatos baseado em co-ocorrência de realizações.
# Params: model (str), concept (str), limit (int)
# Returns: lista de {fqn, name, cooccurrence_count, shared_assets}

# Ferramentas WRITE (passam por proposal → review, igual ao scan):

"propose_add_concepts"
# Adiciona um ou mais conceitos diretamente ao modelo (sem ingestão de lake).
# Params: model (str), terms (list[TermDraft]), note (str)
# TermDraft: {name, definition, broader?, synonyms?, scope_note?, tags?}
# Returns: ProposalResult (mesmo formato de propose_scan)
# Implementação: cv_builder.batch_add_terms() → proposals.py

"propose_add_hierarchy"
# Adiciona um bloco de hierarquia pai→filhos de uma vez.
# Params: model (str), parent (str), children (list[str]), note (str)
# Returns: ProposalResult com AddConcept + skos:broader ops

"propose_add_relation"
# Alias ergonômico para propose_change com AddRelation, mas com validação RT.
# Params: model, from_concept, relation_type (BT|NT|RT|exactMatch|...), to_concept
# Returns: ProposalResult

"propose_deprecate_concept"
# Depreca um conceito e opcionalmente redireciona para substituto.
# Params: model, concept, successor? (str), reason (str)
# Returns: ProposalResult com DeprecateConcept op

"propose_add_scope_note"
# Adiciona skos:scopeNote a um conceito existente.
# Params: model, concept, scope_note, lang? (default "en")
# Returns: ProposalResult com AddScopeNote op
```

---

### M5c — Workflow MAP (Metadata Application Profile)

**Novo módulo `core/map_profile.py`:**

```python
# MAP = contrato semântico entre fonte de dados e ontologia
# Dataclass MapProfile:
#   source_id: str             — fqn da fonte (scan source)
#   model_slug: str            — ontologia alvo
#   canonical_layer: dict      — {campo_fonte: conceito_fqn}
#   implementation_layer: dict — {conceito_fqn: campo_físico_atual}
#   translation_rules: list    — transformações (cast, rename, derive)
#   crosswalks: list[Crosswalk] — mapeamentos para padrões externos
#
# Funções:
# - build_from_scan(scan_id) → MapProfile  (usa resultado de scan existente)
# - validate_map(map_profile) → MapReport  (verifica cobertura e consistência)
# - export_map_markdown(map_profile) → str  (documento legível por humanos)
```

**Novas ferramentas MCP em `server/tools_sources.py` (ou novo `tools_map.py`):**

```python
"build_map"
# Constrói um MAP a partir de um scan existente.
# Params: scan_id (str), model (str)
# Returns: MapProfile como JSON + markdown summary

"validate_map"
# Valida um MAP: cobertura (todos campos mapeados?), consistência (conceitos existem?).
# Params: model (str), source_id (str)
# Returns: MapReport com gaps e avisos

"export_map"
# Exporta o MAP do modelo/fonte como documento Markdown ou JSON.
# Params: model (str), source_id (str), format ("markdown"|"json")
# Returns: documento do MAP
```

---

## Milestone M6 — Knowledge Graph Analytics

### M6a — Entity Linking

**Novo módulo `core/entity_linking.py`:**

```python
# Linking de conceitos do lakeloom para KGs externos
# Funções:
# - lookup_wikidata(term_name, lang="en") → list[WikidataMatch]
#   (usa Wikidata API — wikidata.org/w/api.php, sem autenticação)
# - lookup_schema_org(term_name) → list[SchemaMatch]
# - suggest_external_mappings(model_slug, external_kg) → list[MappingCandidate]
# - propose_external_mappings(candidates) → ProposalResult com AddMapping ops
```

**Nova ferramenta MCP:**

```python
"suggest_external_mappings"
# Busca conceitos do modelo em KGs externos (Wikidata, schema.org).
# Params: model (str), concepts (list[str]|"all"), external_kg ("wikidata"|"schema.org")
# Returns: lista de {concept_fqn, candidate_uri, label, match_score, mapping_kind}
# Requer aprovação antes de propor: retorna candidatos, não proposal ainda

"propose_external_mappings"
# Transforma candidatos aprovados em proposal com AddMapping ops.
# Params: model (str), mappings (list[{concept_fqn, uri, kind}])
# Returns: ProposalResult
```

### M6b — Graph Analytics

**Novo módulo `core/graph_analytics.py`:**

```python
# Análise estrutural do grafo de ontologia
# Funções:
# - concept_stats(model_slug) → ConceptStats
#   {total_concepts, avg_depth, max_depth, isolated_concepts, hub_concepts}
# - hierarchy_report(model_slug) → HierarchyReport
#   {roots, leaves, polyhierarchy_nodes, orphan_concepts}
# - realization_coverage(model_slug) → CoverageStats
#   {concepts_with_realizations, concepts_without, top_realized}
```

**Nova ferramenta MCP:**

```python
"get_model_stats"
# Retorna estatísticas estruturais da ontologia: profundidade, nós órfãos, hubs.
# Params: model (str)
# Returns: ConceptStats + HierarchyReport em markdown
# ToolAnnotations: read_only_hint=True
```

---

## Resumo por Milestone

| Milestone | Escopo | Novos módulos core | Novas ferramentas MCP |
|---|---|---|---|
| **M5a** | Ops diretas + CV builder | `cv_builder.py`, `thesaurus_builder.py`, `coverage.py` | — (apenas ops) |
| **M5b** | Ferramentas MCP CV/Thesaurus | — | `harvest_vocabulary_candidates`, `validate_thesaurus`, `suggest_related_terms`, `propose_add_concepts`, `propose_add_hierarchy`, `propose_add_relation`, `propose_deprecate_concept`, `propose_add_scope_note` |
| **M5c** | MAP workflow | `map_builder.py` | `build_map`, `validate_map`, `export_map` |
| **M6a** | Entity linking KG externo | `entity_linking.py` | `suggest_external_mappings`, `propose_external_mappings` |
| **M6b** | Graph analytics | `graph_analytics.py` | `get_model_stats`, `assess_realization_coverage` |

---

## Sequência de Implementação Recomendada

```
M5a (ops + cv_builder)
  └─► M5b (MCP tools — depende das ops de M5a)
        └─► M5c (MAP — pode rodar em paralelo com M5b)

M6a (entity linking — independente, pode começar após M5b)
  └─► M6b (analytics — independente)
```

---

## Dois Paths de Uso (após implementação)

### Path A — Ingestão do Lake (já existe, enriquecido)

```
lakeloom sources scan <source>          # scan do lake
  → propose_scan (MCP)                  # mapper.py gera operações
  → build_map_from_scan (MCP) [novo]    # gera MAP do scan
  → validate_map (MCP) [novo]           # valida cobertura
  → submit_proposal / review_proposal   # apply via OM
  → validate_thesaurus (MCP) [novo]     # verifica consistência após apply
  → suggest_external_mappings [novo]    # opcional: link para Wikidata
```

### Path B — Criação Direta pelo Usuário

```
create_model (MCP)                      # cria glossário OM
  → propose_add_term (MCP) [novo]       # adiciona conceitos diretamente
  → propose_add_hierarchy (MCP) [novo]  # constrói taxonomia
  → propose_add_relation (MCP) [novo]   # adiciona relações BT/NT/RT
  → propose_add_scope_note (MCP) [novo] # enriquece com scope notes
  → validate_thesaurus (MCP) [novo]     # valida ANSI Z39.19
  → submit_proposal / review_proposal   # apply via OM
  → suggest_external_mappings [novo]    # link para KGs externos
  → graph_stats (MCP) [novo]            # analisa estrutura resultante
```

---

## Decisões de Design a Confirmar

1. **NLP mining**: o livro sugere spaCy/NLTK para extrair candidatos de texto livre. Adicionar como dependência opcional (`lakeloom[nlp]`) ou deixar fora do escopo?
2. **Folksonomy harvest**: `harvest_vocabulary_candidates` proposto usa tags OM existentes. Deve também aceitar texto livre (documentos, wikis) como fonte de candidatos?
3. **MAP como entidade persistida**: o `MapProfile` deve ser salvo no state store (nova tabela Alembic) ou gerado on-demand a partir do scan + modelo?
4. **Wikidata rate limits**: `suggest_external_mappings` chama API externa. Deve ter cache local (SQLite) para evitar chamadas repetidas?
5. **`propose_add_concepts` em lote**: limite de 200 ops por call (igual a `MAX_OPERATIONS_PER_CALL` em `tools_proposals.py`) é adequado para imports de CV grandes?
