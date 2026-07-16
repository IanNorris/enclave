// markdown-it plugin: turn bug ids (e.g. BRO-123) into hoverable/clickable
// references. Bug id prefixes are 2-4 uppercase letters (compute_prefix takes
// the first ~3 alphabetic chars of the session name), followed by -<digits>.
//
// To avoid false positives (ISO-8601, GPT-5, RFC-2822...), a candidate is only
// linkified when `resolve(id)` returns a known bug — i.e. it exists in this
// session's bug list. Matching runs on TEXT tokens only (via the inline token
// tree), so ids inside code spans / fenced blocks are left untouched.

const BUG_RE = /\b[A-Z]{2,4}-\d{1,5}\b/g

export default function bugRefPlugin(md, options = {}) {
  const resolve = typeof options.resolve === 'function' ? options.resolve : () => null

  // Core rule: after inline parsing, split matching ids out of text tokens.
  md.core.ruler.push('bug_ref', (state) => {
    for (const block of state.tokens) {
      if (block.type !== 'inline' || !block.children) continue
      const out = []
      let changed = false
      for (const tok of block.children) {
        if (tok.type !== 'text') { out.push(tok); continue }
        const text = tok.content
        BUG_RE.lastIndex = 0
        let last = 0
        let m
        let hitInThisToken = false
        while ((m = BUG_RE.exec(text)) !== null) {
          const id = m[0]
          if (!resolve(id)) continue // unknown id — leave as plain text
          hitInThisToken = true
          if (m.index > last) {
            const t = new state.Token('text', '', 0)
            t.content = text.slice(last, m.index)
            out.push(t)
          }
          const ref = new state.Token('bug_ref', '', 0)
          ref.content = id
          ref.meta = { id }
          out.push(ref)
          last = m.index + id.length
        }
        if (hitInThisToken) {
          changed = true
          if (last < text.length) {
            const t = new state.Token('text', '', 0)
            t.content = text.slice(last)
            out.push(t)
          }
        } else {
          out.push(tok)
        }
      }
      if (changed) block.children = out
    }
    return false
  })

  md.renderer.rules.bug_ref = (tokens, idx) => {
    const id = md.utils.escapeHtml(tokens[idx].meta.id)
    // Data-only span; the hover card + click navigation are wired in Chat via a
    // delegated listener that reads data-bug-id and looks the bug up live.
    return `<span class="bug-ref" data-bug-id="${id}" role="link" tabindex="0">${id}</span>`
  }
}
