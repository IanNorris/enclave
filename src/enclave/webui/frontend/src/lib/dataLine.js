// markdown-it plugin: tag each block-level element with its source line, so the
// rendered (inert v-html) output can be mapped back to source for inline
// commenting and edit highlighting. markdown-it block tokens carry .map =
// [startLine, endLine]; we stamp data-line onto the opening tokens.
//
// Blocks covered: paragraphs, headings, list items, blockquotes, fenced code,
// and tables. Clicks may land on nested nodes (<td>, <span>) — callers should
// use closest('[data-line]').

const TAGGED = new Set([
  'paragraph_open',
  'heading_open',
  'list_item_open',
  'blockquote_open',
  'table_open',
])

export function dataLinePlugin(md) {
  md.core.ruler.push('enclave_data_line', (state) => {
    for (const tok of state.tokens) {
      if (!tok.map) continue
      if (TAGGED.has(tok.type)) {
        tok.attrSet('data-line', String(tok.map[0]))
      } else if (tok.type === 'fence') {
        // fence is a self-contained token (no _open); render rule reads attrs.
        tok.attrSet('data-line', String(tok.map[0]))
      }
    }
    return true
  })
}

// Slice the frozen block text from raw markdown given a start line (0-based).
// Returns the lines from startLine up to the next blank line or EOF — enough to
// quote the block as context without depending on the next block's start.
export function sliceBlock(source, startLine) {
  const lines = (source || '').split('\n')
  const out = []
  for (let i = startLine; i < lines.length; i++) {
    if (i > startLine && lines[i].trim() === '') break
    out.push(lines[i])
  }
  return out.join('\n').trim()
}
