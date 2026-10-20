; Scopes and bindings, for the highlighters that colour a use by its
; binding (tree-sitter's own `highlight` command and Helix): a use of a
; parameter takes the parameter's colour. The language server's
; semantic tokens resolve names the same way, so this keeps those
; editors and VS Code alike on the uses of a name, not only on its
; binding. Neovim reads this file for navigation, not for colour.
;
; Top-level signatures and macro declarations bind their names in the
; module scope. Forward references and imported names keep their
; syntactic colour where VS Code's server resolves them. Macro parameters
; are not bound here: template uses keep their positional colours.

; ---------------------------------------------------------------
; Scopes
; ---------------------------------------------------------------

(source_file) @local.scope
(function_definition) @local.scope
(lambda) @local.scope
(block) @local.scope
(match_arm) @local.scope
(for_expression) @local.scope
(parallel_expression) @local.scope
(syntax_for_declaration) @local.scope
; Keep the declaration name in module scope, while raw templates do
; not inherit the colours of module functions or macros. Rule names
; already have their syntactic macro capture inside the isolated rule.
((macro_declaration body: (_) @local.scope)
  (#set! local.scope-inherits false))
((macro_rule) @local.scope
  (#set! local.scope-inherits false))

; ---------------------------------------------------------------
; Bindings
; ---------------------------------------------------------------

; A function's own name, for its recursive uses.
(source_file (function_definition name: (identifier) @local.definition))
(source_file (type_signature subject: (identifier) @local.definition))
(source_file (macro_declaration name: (identifier) @local.definition))

(source_file (function_definition parameter: (identifier) @local.definition))
(lambda parameter: (identifier) @local.definition)

; A binding's own value does not see it: in `{ (let ps ps) ps }` the
; initializer uses the `ps` from outside the block.
(local_binding
  name: (identifier) @local.definition
  value: (_) @local.definition-value)
(parallel_binding
  name: (identifier) @local.definition
  value: (_) @local.definition-value)
(for_expression binder: (identifier) @local.definition)
(syntax_for_declaration binder: (identifier) @local.definition)

(match_arm pattern: (identifier) @local.definition)
(constructor_pattern argument: (identifier) @local.definition)
(field_pattern name: (identifier) @local.definition !pattern)
(field_pattern pattern: (identifier) @local.definition)
(parenthesized_pattern pattern: (identifier) @local.definition)
(tuple_pattern element: (identifier) @local.definition)
(list_pattern element: (identifier) @local.definition)

; ---------------------------------------------------------------
; Uses
; ---------------------------------------------------------------

(identifier) @local.reference
