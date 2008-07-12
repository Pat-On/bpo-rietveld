# Copyright 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Diff rendering in HTML for Rietveld."""

# Python imports
import re
import cgi
import difflib
import logging
import urlparse

# AppEngine imports
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db

# Django imports
from django.template import loader

# Local imports
import models
import patching
import intra_region_diff


class FetchError(Exception):
  """Exception raised by FetchBase() when a URL problem occurs."""


def ParsePatchSet(patchset):
  """Patch a patch set into individual patches.

  Args:
    patchset: a models.PatchSet instance.

  Returns:
    A list of models.Patch instances.
  """
  patches = []
  filename = lines = None
  for line in patchset.data.splitlines(True):
    if line.startswith('Index:'):
      if filename and lines:
        patch = models.Patch(patchset=patchset, text=_ToText(lines),
                             filename=filename, parent=patchset)
        patches.append(patch)
      unused, filename = line.split(':', 1)
      filename = filename.strip()
      lines = [line]
      continue
    if lines is not None:
      lines.append(line)
  if filename and lines:
    patch = models.Patch(patchset=patchset, text=_ToText(lines),
                         filename=filename, parent=patchset)
    patches.append(patch)
  return patches


def FetchBase(base, patch):
  """Fetch the content of the file to which the file is relative.

  Args:
    base: the base property of the Issue to which the Patch belongs.
    patch: a models.Patch instance.

  Returns:
    A models.Content instance.

  Raises:
    FetchError: For any kind of problem fetching the content.
  """
  filename, lines = patch.filename, patch.lines
  rev = patching.ParseRevision(lines)
  if rev is not None:
    if rev == 0:
      # rev=0 means it's a new file.
      return models.Content(text=db.Text(u''), parent=patch)
  try:
    base = db.Link(base)
  except db.BadValueError:
    msg = 'Invalid base URL: %s' % base
    logging.error(msg)
    raise FetchError(msg)
  url = _MakeUrl(base, filename, rev)
  logging.info('Fetching %s', url)
  try:
    result = urlfetch.fetch(url)
  except Exception, err:
    msg = 'Error fetching %s: %s: %s' % (url, err.__class__.__name__, err)
    logging.error(msg)
    raise FetchError(msg)
  if result.status_code != 200:
    msg = 'Error fetching %s: HTTP status %s' % (url, result.status_code)
    logging.error(msg)
    raise FetchError(msg)
  return models.Content(text=_ToText([result.content]), parent=patch,
                        is_complete=True)


def _MakeUrl(base, filename, rev):
  """Helper for FetchBase() to construct the URL to fetch.

  Args:
    base: The base property of the Issue to which the Patch belongs.
    filename: The filename property of the Patch instance.
    rev: Revision number, or None for head revision.

  Returns:
    A URL referring to the given revision of the file.
  """
  scheme, netloc, path, params, query, fragment = urlparse.urlparse(base)
  if netloc.endswith(".googlecode.com"):
    # Handle Google code repositories
    assert rev is not None, "Can't access googlecode.com without a revision"
    assert path.startswith("/svn/"), "Malformed googlecode.com URL"
    path = path[5:]  # Strip "/svn/"
    url = "%s://%s/svn-history/r%d/%s/%s" % (scheme, netloc, rev,
                                             path, filename)
    return url
  # Default for viewvc-based URLs (svn.python.org)
  url = base
  if not url.endswith('/'):
    url += '/'
  url += filename
  if rev is not None:
    url += '?rev=%s' % rev
  return url


DEFAULT_CONTEXT = 50

def RenderDiffTableRows(request, old_lines, chunks, patch,
                        colwidth=80, debug=False, context=DEFAULT_CONTEXT):
  """Render the HTML table rows for a side-by-side diff for a patch.

  Args:
    request: Django Request object.
    old_lines: List of lines representing the original file.
    chunks: List of chunks as returned by patching.ParsePatch().
    patch: A models.Patch instance.
    colwidth: Optional column width (default 80).
    debug: Optional debugging flag (default False).
    context: Maximum number of rows surrounding a change (default CONTEXT).

  Yields:
    Strings, each of which represents the text rendering one complete
    pair of lines of the side-by-side diff, possibly including comments.
    Each yielded string may consist of several <tr> elements.
  """
  rows =  _RenderDiffTableRows(request, old_lines, chunks, patch,
                               colwidth, debug)
  return _CleanupTableRowsGenerator(rows, context)


def RenderDiff2TableRows(request, old_lines, old_patch, new_lines, new_patch,
                         colwidth=80, debug=False, context=DEFAULT_CONTEXT):
  """Render the HTML table rows for a side-by-side diff between two patches.

  Args:
    request: Django Request object.
    old_lines: List of lines representing the patched file on the left.
    old_patch: The models.Patch instance corresponding to old_lines.
    new_lines: List of lines representing the patched file on the right.
    new_patch: The models.Patch instance corresponding to new_lines.
    colwidth: Optional column width (default 80).
    debug: Optional debugging flag (default False).
    context: Maximum number of visible context lines (default DEFAULT_CONTEXT).

  Yields:
    Strings, each of which represents the text rendering one complete
    pair of lines of the side-by-side diff, possibly including comments.
    Each yielded string may consist of several <tr> elements.
  """
  rows = _RenderDiff2TableRows(request, old_lines, old_patch,
                               new_lines, new_patch, colwidth, debug)
  return _CleanupTableRowsGenerator(rows, context)


def _CleanupTableRowsGenerator(rows, context):
  """Cleanup rows returned by _TableRowGenerator for output.

  Args:
    rows: List of tuples (tag, text)
    context: Maximum number of visible context lines.

  Yields:
    Rows marked as 'equal' are possibly contracted using _ShortenBuffer().
    Stops on rows marked as 'error'.
  """
  buffer = []
  for tag, text in rows:
    if tag == 'equal':
      buffer.append(text)
      continue
    else:
      for t in _ShortenBuffer(buffer, context):
        yield t
      buffer = []
    yield text
    if tag == 'error':
      yield None
      break
  if buffer:
    for t in _ShortenBuffer(buffer, context):
      yield t


def _ShortenBuffer(buffer, context):
  """Render a possibly contracted series of HTML table rows.

  Args:
    buffer: a list of strings representing HTML table rows.
    context: Maximum number of visible context lines.

  Yields:
    If the buffer has fewer than 3 times context items, yield all
    the items.  Otherwise, yield the first context items, a single
    table row representing the contraction, and the last context
    items.
  """
  if len(buffer) < 3*context:
    for t in buffer:
      yield t
  else:
    last_id = None
    for t in buffer[:context]:
      m = re.match('^<tr( name="hook")? id="pair-(?P<rowcount>\d+)">', t)
      if m:
        last_id = int(m.groupdict().get("rowcount"))
      yield t
    skip = len(buffer) - 2*context
    if skip <= 10:
      expand_link = ('<a href="javascript:M_expandSkipped(%(before)d, '
                     '%(after)d, \'b\', %(skip)d)">Show</a>')
    else:
      expand_link = ('<a href="javascript:M_expandSkipped(%(before)d, '
                     '%(after)d, \'t\', %(skip)d)">Show 10 above</a> '
                     '<a href="javascript:M_expandSkipped(%(before)d, '
                     '%(after)d, \'b\', %(skip)d)">Show 10 below</a> ')
    expand_link = expand_link % {'before': last_id+1,
                                 'after': last_id+skip,
                                 'skip': last_id}
    yield ('<tr id="skip-%d"><td colspan="2" align="center" '
           'style="background:lightblue">'
           '(...skipping <span id="skipcount-%d">%d</span> matching lines...) '
           '<span id="skiplinks-%d">%s</span>'
           '</td></tr>\n' % (last_id, last_id, skip,
                             last_id, expand_link))
    for t in buffer[-context:]:
      yield t


def _RenderDiff2TableRows(request, old_lines, old_patch, new_lines, new_patch,
                         colwidth=80, debug=False):
  """Internal version of RenderDiff2TableRows().

  Args:
    The same as for RenderDiff2TableRows.

  Yields:
    Tuples (tag, row) where tag is an indication of the row type.
  """
  old_dict = {}
  new_dict = {}
  for patch, dct in [(old_patch, old_dict), (new_patch, new_dict)]:
    # XXX GQL doesn't support OR yet...  Otherwise we'd be using that.
    for comment in models.Comment.gql(
        'WHERE patch = :1 AND left = FALSE ORDER BY date', patch):
      if comment.draft and comment.author != request.user:
        continue  # Only show your own drafts
      comment.complete(patch)
      lst = dct.setdefault(comment.lineno, [])
      lst.append(comment)
  return _TableRowGenerator(old_patch, old_dict, len(old_lines)+1, 'new',
                            new_patch, new_dict, len(new_lines)+1, 'new',
                            _GenerateTriples(old_lines, new_lines),
                            colwidth, debug)


def _GenerateTriples(old_lines, new_lines):
  """Helper for _RenderDiff2TableRows yielding input for _TableRowGenerator.

  Args:
    old_lines: List of lines representing the patched file on the left.
    new_lines: List of lines representing the patched file on the right.

  Yields:
    Tuples (tag, old_slice, new_slice) where tag is a tag as returned by
    difflib.SequenceMatchser.get_opcodes(), and old_slice and new_slice
    are lists of lines taken from old_lines and new_lines.
  """
  sm = difflib.SequenceMatcher(None, old_lines, new_lines)
  for tag, i1, i2, j1, j2 in sm.get_opcodes():
    yield tag, old_lines[i1:i2], new_lines[j1:j2]


def _RenderDiffTableRows(request, old_lines, chunks, patch,
                         colwidth=80, debug=False):
  """Internal version of RenderDiffTableRows().

  Args:
    The same as for RenderDiffTableRows.

  Yields:
    Tuples (tag, row) where tag is an indication of the row type.
  """
  old_dict = {}
  new_dict = {}
  if patch:
    # XXX GQL doesn't support OR yet...  Otherwise we'd be using
    # .gql('WHERE patch = :1 AND (draft = FALSE OR author = :2) ORDER BY data',
    #      patch, request.user)
    for comment in models.Comment.gql('WHERE patch = :1 ORDER BY date', patch):
      if comment.draft and comment.author != request.user:
        continue  # Only show your own drafts
      comment.complete(patch)
      if comment.left:
        dct = old_dict
      else:
        dct = new_dict
      lst = dct.setdefault(comment.lineno, [])
      lst.append(comment)
  old_max, new_max = _ComputeLineCounts(old_lines, chunks)
  return _TableRowGenerator(patch, old_dict, old_max, 'old',
                            patch, new_dict, new_max, 'new',
                            patching.PatchChunks(old_lines, chunks),
                            colwidth, debug)


def _TableRowGenerator(old_patch, old_dict, old_max, old_snapshot,
                       new_patch, new_dict, new_max, new_snapshot,
                       triple_iterator, colwidth=80, debug=False):
  """Helper function to render side-by-side table rows.

  Args:
    old_patch: First models.Patch instance.
    old_dict: Dictionary with line numbers as keys and comments as values (left)
    old_max: Line count of the patch on the left.
    old_snapshot: A tag used in the comments form.
    new_patch: Second models.Patch instance.
    new_dict: Same as old_dict, but for the right side.
    new_max: Line count of the patch on the right.
    new_snapshot: A tag used in the comments form.
    triple_iterator: Iterator that yields (tag, old, new) triples.
    colwidth: Optional column width (default 80).
    debug: Optional debugging flag (default False).

  Yields:
    Tuples (tag, row) where tag is an indication of the row type and
    row is an HTML fragment representing one or more <td> elements.
  """
  diff_params = intra_region_diff.GetDiffParams(dbg=debug)
  ndigits = 1 + max(len(str(old_max)), len(str(new_max)))
  indent = 1 + ndigits
  old_offset = new_offset = 0
  row_count = 0

  # Render a row with a message if a side is empty or both sides are equal.
  if old_patch == new_patch and (old_max == 0 or new_max == 0):
    if old_max == 0:
      msg_old = '(Empty)'
    else:
      msg_old = ''
    if new_max == 0:
      msg_new = '(Empty)'
    else:
      msg_new = ''
    yield '', ('<tr><td class="info">%s</td>'
               '<td class="info">%s</td></tr>' % (msg_old, msg_new))
  elif old_patch != new_patch and old_patch.lines == new_patch.lines:
    yield '', ('<tr><td class="info" colspan="2">'
               '(Both sides are equal)</td></tr>')

  for tag, old, new in triple_iterator:
    if tag.startswith('error'):
      yield 'error', '<tr><td><h3>%s</h3></td></tr>\n' % cgi.escape(tag)
      return
    old1 = old_offset
    old_offset = old2 = old1 + len(old)
    new1 = new_offset
    new_offset = new2 = new1 + len(new)
    old_buff = []
    new_buff = []
    frag_list = []
    do_ir_diff = tag == 'replace' and intra_region_diff.CanDoIRDiff(old, new)

    for i in xrange(max(len(old), len(new))):
      row_count += 1
      old_lineno = old1 + i + 1
      new_lineno = new1 + i + 1
      old_valid = old1+i < old2
      new_valid = new1+i < new2

      # Start rendering the first row
      frags = []
      if i == 0 and tag != 'equal':
        # Mark the first row of each non-equal chunk as a 'hook'.
        frags.append('<tr name="hook"')
      else:
        frags.append('<tr')
      frags.append(' id="pair-%d">' % row_count)

      old_intra_diff = ''
      new_intra_diff = ''
      if old_valid:
        old_intra_diff = old[i]
      if new_valid:
        new_intra_diff = new[i]

      frag_list.append(frags)
      if do_ir_diff:
        # Don't render yet. Keep saving state necessary to render the whole
        # region until we have encountered all the lines in the region.
        old_buff.append([old_valid, old_lineno, old_intra_diff])
        new_buff.append([new_valid, new_lineno, new_intra_diff])
      else:
        # We render line by line as usual if do_ir_diff is false
        old_intra_diff = intra_region_diff.Fold(
          old_intra_diff, colwidth + indent, indent, indent)
        new_intra_diff = intra_region_diff.Fold(
          new_intra_diff, colwidth + indent, indent, indent)
        old_buff_out = [[old_valid, old_lineno,
                         (old_intra_diff, True, None)]]
        new_buff_out = [[new_valid, new_lineno,
                         (new_intra_diff, True, None)]]
        for tg, frag in _RenderDiffInternal(old_buff_out, new_buff_out,
                                            ndigits, tag, frag_list,
                                            do_ir_diff,
                                            old_dict, new_dict,
                                            old_patch, new_patch,
                                            old_snapshot, new_snapshot,
                                            colwidth, debug):
          yield tg, frag
        frag_list = []

    if do_ir_diff:
      # So this was a replace block which means that the whole region still
      # needs to be rendered.
      old_lines = [b[2] for b in old_buff]
      new_lines = [b[2] for b in new_buff]
      ret = intra_region_diff.IntraRegionDiff(old_lines, new_lines,
                                              diff_params)
      old_chunks, new_chunks, ratio = ret
      old_tag = 'old'
      new_tag = 'new'

      old_diff_out = intra_region_diff.RenderIntraRegionDiff(
        old_lines, old_chunks, old_tag, ratio,
        limit=colwidth, indent=indent,
        dbg=debug)
      new_diff_out = intra_region_diff.RenderIntraRegionDiff(
        new_lines, new_chunks, new_tag, ratio,
        limit=colwidth, indent=indent,
        dbg=debug)
      for (i, b) in enumerate(old_buff):
        b[2] = old_diff_out[i]
      for (i, b) in enumerate(new_buff):
        b[2] = new_diff_out[i]

      for tg, frag in _RenderDiffInternal(old_buff, new_buff,
                                          ndigits, tag, frag_list,
                                          do_ir_diff,
                                          old_dict, new_dict,
                                          old_patch, new_patch,
                                          old_snapshot, new_snapshot,
                                          colwidth, debug):
        yield tg, frag
      old_buff = []
      new_buff = []


def _CleanupTableRows(rows):
  """Cleanup rows returned by _TableRowGenerator.

  Args:
    rows: Sequence of (tag, text) tuples.

  Yields:
    Rows marked as 'equal' are possibly contracted using _ShortenBuffer().
    Stops on rows marked as 'error'.
  """
  buffer = []
  for tag, text in rows:
    if tag == 'equal':
      buffer.append(text)
      continue
    else:
      for t in _ShortenBuffer(buffer):
        yield t
      buffer = []
    yield text
    if tag == 'error':
      yield None
      break
  if buffer:
    for t in _ShortenBuffer(buffer):
      yield t


def _RenderDiffInternal(old_buff, new_buff, ndigits, tag, frag_list,
                        do_ir_diff, old_dict, new_dict,
                        old_patch, new_patch,
                        old_snapshot, new_snapshot,
                        colwidth, debug):
  """Helper for _TableRowGenerator()."""
  obegin = (intra_region_diff.BEGIN_TAG %
            intra_region_diff.COLOR_SCHEME['old']['match'])
  nbegin = (intra_region_diff.BEGIN_TAG %
            intra_region_diff.COLOR_SCHEME['new']['match'])
  oend = intra_region_diff.END_TAG
  nend = oend
  user = users.get_current_user()

  for i in xrange(len(old_buff)):
    tg = tag
    old_valid, old_lineno, old_out = old_buff[i]
    new_valid, new_lineno, new_out = new_buff[i]
    old_intra_diff, old_has_newline, old_debug_info = old_out
    new_intra_diff, new_has_newline, new_debug_info = new_out

    frags = frag_list[i]
    # Render left text column
    frags.append(_RenderDiffColumn(old_patch, old_valid, tag, ndigits,
                                   old_lineno, obegin, oend, old_intra_diff,
                                   do_ir_diff, old_has_newline, 'old'))

    # Render right text column
    frags.append(_RenderDiffColumn(new_patch, new_valid, tag, ndigits,
                                   new_lineno, nbegin, nend, new_intra_diff,
                                   do_ir_diff, new_has_newline, 'new'))

    # End rendering the first row
    frags.append('</tr>\n')

    if debug:
      frags.append('<tr>')
      if old_debug_info:
        frags.append('<td class="debug-info">%s</td>' %
                     old_debug_info.replace('\n', '<br>'))
      else:
        frags.append('<td></td>')
      if new_debug_info:
        frags.append('<td class="debug-info">%s</td>' %
                     new_debug_info.replace('\n', '<br>'))
      else:
        frags.append('<td></td>')
      frags.append('</tr>\n')

    if old_patch or new_patch:
      # Start rendering the second row
      if ((old_valid and old_lineno in old_dict) or
          (new_valid and new_lineno in new_dict)):
        tg += '_comment'
        frags.append('<tr class="inline-comments" name="hook">')
      else:
        frags.append('<tr class="inline-comments">')

      # Render left inline comments
      frags.append(_RenderInlineComments(old_valid, old_lineno, old_dict,
                                         user, old_patch, old_snapshot, 'old'))

      # Render right inline comments
      frags.append(_RenderInlineComments(new_valid, new_lineno, new_dict,
                                         user, new_patch, new_snapshot, 'new'))

      # End rendering the second row
      frags.append('</tr>\n')

    # Yield the combined fragments
    yield tg, ''.join(frags)


def _RenderDiffColumn(patch, line_valid, tag, ndigits, lineno, begin, end,
                      intra_diff, do_ir_diff, has_newline, prefix):
  """Helper function for _RenderDiffInternal().

  Returns:
    A rendered column.
  """
  if line_valid:
    cls_attr = '%s%s' % (prefix, tag)
    if tag == 'equal':
      lno = '%*d' % (ndigits, lineno)
    else:
      lno = _MarkupNumber(ndigits, lineno, 'u')
    if tag == 'replace':
      col_content = ('%s%s %s%s' % (begin, lno, end, intra_diff))
      # If IR diff has been turned off or there is no matching new line at
      # the end then switch to dark background CSS style.
      if not do_ir_diff or not has_newline:
        cls_attr = cls_attr + '1'
    else:
      col_content = '%s %s' % (lno, intra_diff)
    return '<td class="%s" id="%scode%d">%s</td>' % (cls_attr, prefix,
                                                     lineno, col_content)
  else:
    return '<td class="%sblank"></td>' % prefix


def _RenderInlineComments(line_valid, lineno, data, user,
                          patch, snapshot, prefix):
  """Helper function for _RenderDiffInternal().

  Returns:
    Rendered comments.
  """
  comments = []
  if line_valid:
    comments.append('<td id="%s-line-%s">' % (prefix, lineno))
    if lineno in data:
      comments.append(
        _ExpandTemplate('inline_comment.html',
                        user=user,
                        patch=patch,
                        patchset=patch.patchset,
                        issue=patch.patchset.issue,
                        snapshot=snapshot,
                        side='a' if prefix == 'old' else 'b',
                        comments=data[lineno],
                        lineno=lineno,
                        ))
    comments.append('</td>')
  else:
    comments.append('<td></td>')
  return ''.join(comments)


def _ComputeLineCounts(old_lines, chunks):
  """Compute the length of the old and new sides of a diff.

  Args:
    old_lines: List of lines representing the original file.
    chunks: List of chunks as returned by patching.ParsePatch().

  Returns:
    A tuple (old_len, new_len) representing len(old_lines) and
    len(new_lines), where new_lines is the list representing the
    result of applying the patch chunks to old_lines, however, without
    actually computing new_lines.
  """
  old_len = len(old_lines)
  new_len = old_len
  if chunks:
    (old_a, old_b), (new_a, new_b), old_lines, new_lines = chunks[-1]
    new_len += new_b - old_b
  return old_len, new_len


def _MarkupNumber(ndigits, number, tag):
  """Format a number in HTML in a given width with extra markup.

  Args:
    ndigits: the total width available for formatting
    number: the number to be formatted
    tag: HTML tag name, e.g. 'u'

  Returns:
    An HTML string that displays as ndigits wide, with the
    number right-aligned and surrounded by an HTML tag; for example,
    _MarkupNumber(42, 4, 'u') returns '  <u>42</u>'.
  """
  formatted_number = str(number)
  space_prefix = ' ' * (ndigits - len(formatted_number))
  return '%s<%s>%s</%s>' % (space_prefix, tag, formatted_number, tag)


def _ExpandTemplate(name, **params):
  """Wrapper around django.template.loader.render_to_string().

  For convenience, this takes keyword arguments instead of a dict.
  """
  return loader.render_to_string(name, params)


def _ToText(lines):
  """Helper to turn a list of lines into a db.Text instance.

  Args:
    lines: list of strings.

  Returns:
    A db.Text instance.
  """
  text = ''.join(lines)
  try:
    return db.Text(text, encoding='utf-8')
  except UnicodeDecodeError:
    return db.Text(text, encoding='latin-1')
