// markdown-it plugin: render TeX math with KaTeX.
//
// Supports inline `$ ... $` and block `$$ ... $$` (the block form may span
// multiple lines). Adapted from the widely-used markdown-it-katex algorithm,
// which is careful about the ambiguous `$`: an inline `$` only opens math when
// it is not followed by whitespace and not an escaped `\$`, and a lone `$5 and
// $10` in prose is left as literal text. Rendering failures fall back to the
// raw source (in an error-styled span) instead of throwing, so a malformed
// expression never blanks the whole message.
import katex from 'katex'

function isValidDelim(state, pos) {
  const prevChar = pos > 0 ? state.src.charCodeAt(pos - 1) : -1
  const nextChar = pos + 1 <= state.posMax ? state.src.charCodeAt(pos + 1) : -1
  const canOpen = !(nextChar === 0x20 /* space */ || nextChar === 0x09 /* tab */)
  const canClose = !(prevChar === 0x20 || prevChar === 0x09)
  // Not a delimiter if the next char is a digit (e.g. "$5") — prose currency.
  const nextIsDigit = nextChar >= 0x30 && nextChar <= 0x39
  return { canOpen: canOpen && !nextIsDigit, canClose }
}

function inlineMath(state, silent) {
  if (state.src[state.pos] !== '$') return false

  const res = isValidDelim(state, state.pos)
  if (!res.canOpen) {
    if (!silent) state.pending += '$'
    state.pos += 1
    return true
  }

  const start = state.pos + 1
  let match = start
  let pos
  while ((match = state.src.indexOf('$', match)) !== -1) {
    pos = match - 1
    while (state.src[pos] === '\\') pos -= 1
    // Even number of backslashes => the `$` is unescaped.
    if ((match - pos) % 2 === 1) break
    match += 1
  }

  if (match === -1) {
    if (!silent) state.pending += '$'
    state.pos = start
    return true
  }
  if (match - start === 0) {
    // Empty `$$` inline — treat as literal.
    if (!silent) state.pending += '$$'
    state.pos = start + 1
    return true
  }

  const closeRes = isValidDelim(state, match)
  if (!closeRes.canClose) {
    if (!silent) state.pending += '$'
    state.pos = start
    return true
  }

  if (!silent) {
    const token = state.push('math_inline', 'math', 0)
    token.markup = '$'
    token.content = state.src.slice(start, match)
  }
  state.pos = match + 1
  return true
}

function blockMath(state, startLine, endLine, silent) {
  let pos = state.bMarks[startLine] + state.tShift[startLine]
  let max = state.eMarks[startLine]

  if (pos + 2 > max) return false
  if (state.src.slice(pos, pos + 2) !== '$$') return false

  pos += 2
  let firstLine = state.src.slice(pos, max)
  if (silent) return true

  let lastLine
  let found = false
  if (firstLine.trim().slice(-2) === '$$') {
    firstLine = firstLine.trim().slice(0, -2)
    found = true
  }

  let next = startLine
  for (; !found; ) {
    next += 1
    if (next >= endLine) break
    pos = state.bMarks[next] + state.tShift[next]
    max = state.eMarks[next]
    if (pos < max && state.tShift[next] < state.blkIndent) break
    if (state.src.slice(pos, max).trim().slice(-2) === '$$') {
      const lastPos = state.src.slice(0, max).lastIndexOf('$$')
      lastLine = state.src.slice(pos, lastPos)
      found = true
    }
  }

  state.line = next + 1

  const token = state.push('math_block', 'math', 0)
  token.block = true
  token.content =
    (firstLine && firstLine.trim() ? firstLine + '\n' : '') +
    state.getLines(startLine + 1, next, state.tShift[startLine], true) +
    (lastLine && lastLine.trim() ? lastLine : '')
  token.map = [startLine, state.line]
  token.markup = '$$'
  return true
}

export default function mathPlugin(md, options = {}) {
  const katexOptions = { throwOnError: false, ...options }

  const renderInline = (tex) => {
    try {
      return katex.renderToString(tex, { ...katexOptions, displayMode: false })
    } catch (e) {
      return `<span class="katex-error" title="${md.utils.escapeHtml(String(e))}">${md.utils.escapeHtml(tex)}</span>`
    }
  }
  const renderBlock = (tex) => {
    try {
      return `<p>${katex.renderToString(tex, { ...katexOptions, displayMode: true })}</p>`
    } catch (e) {
      return `<p class="katex-error" title="${md.utils.escapeHtml(String(e))}">${md.utils.escapeHtml(tex)}</p>`
    }
  }

  md.inline.ruler.after('escape', 'math_inline', inlineMath)
  md.block.ruler.after('blockquote', 'math_block', blockMath, {
    alt: ['paragraph', 'reference', 'blockquote', 'list'],
  })
  md.renderer.rules.math_inline = (tokens, idx) => renderInline(tokens[idx].content)
  md.renderer.rules.math_block = (tokens, idx) => renderBlock(tokens[idx].content) + '\n'
}
