import React, { useState, useEffect, useRef } from 'react'

// Backend URL. Set VITE_API_BASE at build time (see .env / docker-compose.yml).
// Falls back to the current origin, which works when a reverse proxy serves
// the API and the SPA from the same host.
const API_BASE = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/+$/, '')

// --- helpers ---
function validateISBN(isbn){
  if(!isbn) return true
  const cleaned = isbn.replace(/[-\s]/g,'')
  return /^(?:\d{10}|\d{13})$/.test(cleaned)
}

// inputs are trimmed at point of use so stray whitespace never reaches the API
const t = (s)=> (s==null ? '' : String(s).trim())

function formatAdded(value){
  if(!value) return '—'
  const d = new Date(value)
  if(isNaN(d.getTime())) return value
  // Timestamps are stored in UTC; render them in UTC so the displayed day always
  // matches the value shown in the date editor.
  return d.toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric', timeZone:'UTC'})
}

// value for <input type="date">, taken from the UTC parts of the stored timestamp
function toDateInput(value){
  if(!value) return ''
  const d = new Date(value)
  if(isNaN(d.getTime())) return ''
  const pad = n=> String(n).padStart(2,'0')
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())}`
}

function friendlyMessage(raw){
  const text = typeof raw === 'string' ? raw : JSON.stringify(raw || '')
  if(/UNIQUE constraint failed: books\.isbn/i.test(text)) return 'Error: book already in database!'
  if(/UNIQUE constraint failed/i.test(text)) return 'Error: this book is already in the database!'
  try{
    const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw
    if(parsed && parsed.detail) return typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail)
  }catch(_){ /* not json */ }
  return text
}

async function readError(res){
  let body = ''
  try{ body = JSON.stringify(await res.json()) }catch(_){ try{ body = await res.text() }catch(__){ body = `HTTP ${res.status}` } }
  return friendlyMessage(body)
}

function authHeaders(json){
  const token = localStorage.getItem('token')
  const h = token ? {Authorization: 'Bearer ' + token} : {}
  if(json) h['Content-Type'] = 'application/json'
  return h
}

// <img> cannot send an Authorization header, so the token rides along in the query
// string. cacheBust changes whenever a cover is replaced so the browser refetches.
function coverUrl(book, cacheBust){
  const token = localStorage.getItem('token') || ''
  return `${API_BASE}/books/${book.id}/cover?token=${encodeURIComponent(token)}&v=${cacheBust||0}`
}

function Login({onLogin}){
  const [username,setUsername]=useState('')
  const [password,setPassword]=useState('')
  const [error,setError]=useState(null)
  const submit=async e=>{
    e.preventDefault()
    setError(null)
    const body=new URLSearchParams(); body.append('username',t(username)); body.append('password',password); body.append('grant_type','')
    const res = await fetch(API_BASE + '/token',{method:'POST', body})
    if(!res.ok){ setError('Login failed - check username and password'); return }
    const j = await res.json(); localStorage.setItem('token', j.access_token); onLogin();
  }
  return (
    <form onSubmit={submit} className="card">
      <h3>Login</h3>
      {error && <div className="alert">{error}</div>}
      <label>Username<input value={username} onChange={e=>setUsername(e.target.value)}/></label>
      <label>Password<input type="password" value={password} onChange={e=>setPassword(e.target.value)}/></label>
      <button type="submit">Login</button>
    </form>
  )
}

function AddForm({onAdded}){
  const [title,setTitle]=useState('')
  const [author,setAuthor]=useState('')
  const [isbn,setIsbn]=useState('')
  const [olid,setOlid]=useState('')
  const [googleId,setGoogleId]=useState('')
  const [notes,setNotes]=useState('')
  const [error,setError]=useState(null)
  const [loading,setLoading]=useState(false)
  const [searching,setSearching]=useState(false)
  const [searchResults,setSearchResults]=useState([])
  const [expandedSet,setExpandedSet]=useState({})
  const [allLanguages,setAllLanguages]=useState(false)
  const toggleExpanded = (idx)=> setExpandedSet(prev=> ({...prev, [idx]: !prev[idx]}))

  const submit=async e=>{
    if(e) e.preventDefault()
    setError(null)
    const vals = {title: t(title), author: t(author), isbn: t(isbn), olid: t(olid), google_id: t(googleId), notes: t(notes)}
    const missing = []
    if(!vals.title) missing.push('Title')
    if(!vals.author) missing.push('Author')
    if(!vals.isbn) missing.push('ISBN')
    if(missing.length){ setError(`${missing.join(', ')} ${missing.length>1?'are':'is'} required to add a book manually`); return }
    if(!validateISBN(vals.isbn)){ setError('ISBN must be 10 or 13 digits'); return }
    if(vals.olid && !/^OL\d+M$/i.test(vals.olid)){ setError('OLID must look like OL12345M'); return }
    try{
      const res = await fetch(API_BASE + '/books',{method:'POST', headers: authHeaders(true), body: JSON.stringify(vals)})
      if(!res.ok){ setError(await readError(res)); return }
      const created = await res.json()
      setTitle(''); setAuthor(''); setIsbn(''); setOlid(''); setGoogleId(''); setNotes('')
      setSearchResults([])
      onAdded(created)
    }catch(err){
      setError(friendlyMessage(err.message))
    }
  }

  const lookup = async ()=>{
    const isbnVal = t(isbn)
    if(!isbnVal){ setError('Enter ISBN first'); return }
    setIsbn(isbnVal)
    setError(null); setLoading(true)
    try{
      const res = await fetch(API_BASE + '/lookup/' + encodeURIComponent(isbnVal), {headers: authHeaders()})
      if(!res.ok){ setError('Lookup failed'); setLoading(false); return }
      const j = await res.json()
      const foundTitle = j.title || ''
      const foundAuthor = (j.authors && j.authors.length) ? j.authors.join(', ') : ''
      if(foundTitle) setTitle(foundTitle)
      if(foundAuthor) setAuthor(foundAuthor)
      if(j.olid) setOlid(j.olid)
      if(j.google_id) setGoogleId(j.google_id)
      if(!foundTitle && !foundAuthor && !j.olid){ setError('No data found'); setLoading(false); return }
      setLoading(false)
      // Chain straight into the edition search: the point of a lookup is almost
      // always to then pick the right edition, and the ISBN we just resolved
      // highlights the matching one. Search on what the lookup returned rather
      // than on the form, so a lookup that only resolves an OLID cannot search
      // using the previous book's title and quietly return the wrong editions.
      if(foundTitle || foundAuthor){
        await searchMeta(undefined, {title: foundTitle, author: foundAuthor})
      }
      return
    }catch(err){ setError(err.message) }
    setLoading(false)
  }

  const searchMeta = async (includeAll, overrides)=>{
    const titleVal = t(overrides && overrides.title !== undefined ? overrides.title : title)
    const authorVal = t(overrides && overrides.author !== undefined ? overrides.author : author)
    if(!titleVal && !authorVal){ setError('Enter title or author to search'); return }
    const all = includeAll===undefined ? allLanguages : includeAll
    setError(null); setSearching(true); setSearchResults([])
    try{
      const params = []
      if(titleVal) params.push('title=' + encodeURIComponent(titleVal))
      if(authorVal) params.push('author=' + encodeURIComponent(authorVal))
      if(all) params.push('include_all_languages=true')
      const url = API_BASE + '/search' + (params.length? '?' + params.join('&') : '')
      const res = await fetch(url, {headers: authHeaders()})
      if(!res.ok){ setError('Search failed'); setSearching(false); return }
      const j = await res.json()
      setSearchResults(j)
      if(j.length===0) setError('No matching books found on OpenLibrary or Google Books')
    }catch(err){ setError(err.message) }
    setSearching(false)
  }

  const handleAddFromSearch = async (doc, isbnVal, olidVal, coverVal, googleVal) =>{
    let details = null
    try{
      if(olidVal){
        const res = await fetch(API_BASE + '/edition/' + encodeURIComponent(olidVal), {headers: authHeaders()})
        if(res.ok) details = await res.json()
      } else if(isbnVal){
        const res = await fetch(API_BASE + '/lookup/' + encodeURIComponent(isbnVal), {headers: authHeaders()})
        if(res.ok) details = await res.json()
      }
    }catch(e){ console.error('fetch edition failed', e) }

    const titleVal = t((details && details.title) || doc.title)
    const authorsVal = t((details && details.authors && details.authors.join(', ')) || (doc.authors && doc.authors.join(', ')))
    const pub = (details && details.publish_date) || doc.publish_year || ''
    const display = `${titleVal}${authorsVal? ' — ' + authorsVal: ''}${pub? ' ('+pub+')':''}`
    if(!confirm('Add this edition to your library?\n\n' + display)) return

    const payload = { title: titleVal, author: authorsVal, isbn: t(isbnVal || (details && details.isbns && details.isbns[0])), olid: olidVal || null, google_id: googleVal || (details && details.google_id) || null, notes: '', cover_url: coverVal || null }
    try{
      const r = await fetch(API_BASE + '/books', {method: 'POST', headers: authHeaders(true), body: JSON.stringify(payload)})
      if(!r.ok){ setError(await readError(r)); return }
      const created = await r.json()
      setError(null)
      onAdded(created)
      setSearchResults([])
    }catch(err){ setError(friendlyMessage(err.message)) }
  }

  return (
    <div className="card" style={{minWidth:0}}>
      <form onSubmit={e=>{ e.preventDefault(); searchMeta() }}>
        <h3>Add book</h3>
        {error && <div className="alert">{error}</div>}
        <label>Title<input value={title} onChange={e=>setTitle(e.target.value)}/></label>
        <label>Author<input value={author} onChange={e=>setAuthor(e.target.value)}/></label>
        <div style={{margin:'8px 0',display:'flex',gap:10,alignItems:'center',flexWrap:'wrap'}}>
          <button type="submit" className="primary" disabled={searching || loading}>{searching? 'Searching...':'Search editions'}</button>
          <label className="inline-check">
            <input type="checkbox" checked={allLanguages} onChange={e=>{ setAllLanguages(e.target.checked); if(searchResults.length) searchMeta(e.target.checked) }} />
            Include translations
          </label>
        </div>
        <label>ISBN
          <div style={{display:'flex',gap:6}}>
            <input style={{flex:1,minWidth:0}} value={isbn} onChange={e=>setIsbn(e.target.value)}/>
            <button type="button" onClick={lookup} disabled={loading}>{loading? 'Looking...':'Lookup ISBN'}</button>
          </div>
        </label>
        <label>OLID <span className="hint">(optional — filled in automatically when you add from search)</span>
          <input value={olid} placeholder="OL12345M" onChange={e=>setOlid(e.target.value)}/>
        </label>
        <label>Google ID <span className="hint">(optional — makes later lookups faster and exact)</span>
          <input value={googleId} placeholder="otCEEQAAQBAJ" onChange={e=>setGoogleId(e.target.value)}/>
        </label>
        <label>Notes<input value={notes} onChange={e=>setNotes(e.target.value)}/></label>
        <div style={{marginTop:8}}>
          <button type="button" className="danger" onClick={()=>submit()}>Manually add</button>
        </div>
      </form>

      {searchResults && searchResults.length>0 && (
        <div style={{marginTop:12,minWidth:0}}>
          <h4>Search results</h4>
          {searchResults.map((doc,idx)=> (
            <div key={idx} style={{border:'1px solid #ddd',borderRadius:6,padding:8,marginBottom:6,minWidth:0}}>
              <div style={{fontWeight:600}}>{doc.title} {doc.publish_year? `(${doc.publish_year})`: ''}</div>
              <div style={{fontSize:13,color:'#444'}}>{(doc.authors||[]).join(', ')}</div>
              <div style={{marginTop:6,minWidth:0}}>
                <div style={{fontSize:13,color:'#666'}}>
                  Editions:
                  {doc.source==='google' && <span className="source-badge">via Google Books</span>}
                </div>
                {(doc.editions && doc.editions.length>0) ? (
                  <>
                    <div className="edition-scroller">
                      {((expandedSet[idx]) ? doc.editions : doc.editions.slice(0,6)).map((ed,ii)=> {
                        const wanted = t(isbn).replace(/[-\s]/g,'')
                        const match = wanted && (ed.isbns||[]).some(x=> String(x).replace(/[-\s]/g,'')===wanted)
                        return (
                          <button key={ed.olid || ed.isbns?.[0] || ii} type="button" className={match? 'edition-card match':'edition-card'} onClick={()=>handleAddFromSearch(doc, (ed.isbns && ed.isbns[0]) || null, ed.olid, ed.cover, ed.google_id)}>
                            {ed.cover
                              ? <img src={ed.cover} alt="cover" className="edition-cover"/>
                              : <div className="edition-cover edition-cover-empty">No cover</div>}
                            <div className="edition-title">{ed.title}</div>
                            <div className="edition-meta">{ed.publish_date || ''}{ed.publishers && ed.publishers.length? ' — '+ed.publishers[0]: ''}</div>
                            <div className="edition-meta">{[ed.format, ed.number_of_pages? ed.number_of_pages+' pages':null].filter(Boolean).join(' — ')}</div>
                            {(ed.isbns && ed.isbns.length)
                              ? <div className="edition-isbn">ISBN {ed.isbns[0]}</div>
                              : <div className="edition-isbn edition-isbn-none">No ISBN</div>}
                          </button>
                        )
                      })}
                    </div>
                    {doc.editions.length>6 && (
                      <div style={{marginTop:6}}>
                        <button type="button" onClick={()=>toggleExpanded(idx)}>{expandedSet[idx] ? 'Show less' : `Show all ${doc.editions.length} editions`}</button>
                      </div>
                    )}
                  </>
                ) : (
                  <div style={{display:'flex',gap:6,flexWrap:'wrap',marginTop:6}}>
                    {(doc.isbns||[]).slice(0,6).map((ib,ii)=> (
                      <button key={ii} type="button" onClick={()=>handleAddFromSearch(doc, ib, null)}>{ib}</button>
                    ))}
                    {(doc.edition_keys||[]).slice(0,6).map((ek,ii)=> (
                      <button key={"ek"+ii} type="button" onClick={()=>handleAddFromSearch(doc, null, ek)}>{ek}</button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function CoverCell({book, onChanged, onError}){
  const [busy,setBusy]=useState(false)
  const [version,setVersion]=useState(0)
  const fileRef = useRef(null)

  const apply = (updated)=>{ setVersion(v=>v+1); if(onChanged) onChanged(updated) }

  const lookup = async ()=>{
    setBusy(true); onError(null)
    try{
      const res = await fetch(API_BASE + '/books/' + book.id + '/cover/lookup', {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else apply(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  const upload = async (file)=>{
    if(!file) return
    setBusy(true); onError(null)
    try{
      const body = new FormData(); body.append('file', file)
      const res = await fetch(API_BASE + '/books/' + book.id + '/cover', {method:'POST', headers: authHeaders(), body})
      if(!res.ok){ onError(await readError(res)) }
      else apply(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
    if(fileRef.current) fileRef.current.value = ''
  }

  const fromUrl = async ()=>{
    const entered = prompt('Paste the web address of a cover image')
    if(entered === null) return
    const url = entered.trim()
    if(!url) return
    setBusy(true); onError(null)
    try{
      const res = await fetch(API_BASE + '/books/' + book.id + '/cover/lookup?cover_url=' + encodeURIComponent(url), {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else apply(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  const remove = async ()=>{
    if(!confirm(`Remove the cover for "${book.title}"?`)) return
    setBusy(true); onError(null)
    try{
      const res = await fetch(API_BASE + '/books/' + book.id + '/cover', {method:'DELETE', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else apply(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  return (
    <div className="cover-cell">
      {book.has_cover
        ? <img className="cover-thumb" src={coverUrl(book, version)} alt={`Cover of ${book.title}`} />
        : <div className="cover-thumb cover-thumb-empty">No cover</div>}
      <div className="cover-actions">
        {!book.has_cover && <button type="button" onClick={lookup} disabled={busy}>{busy? '...' : 'Lookup'}</button>}
        {book.has_cover && <button type="button" onClick={remove} disabled={busy}>Remove</button>}
        <button type="button" onClick={()=>fileRef.current && fileRef.current.click()} disabled={busy}>Upload</button>
        <button type="button" onClick={fromUrl} disabled={busy}>From URL</button>
        <input ref={fileRef} type="file" accept="image/*" style={{display:'none'}} onChange={e=>upload(e.target.files && e.target.files[0])} />
      </div>
    </div>
  )
}

function TagFilter({tags, selected, match, onToggle, onMatchChange, onClear, onRefreshAll, refreshing}){
  const [showAll,setShowAll] = useState(false)
  if((!tags || tags.length===0) && !onRefreshAll) return null
  // Most used first, but keep selected tags visible even when the list is capped.
  const ordered = [...(tags||[])].sort((a,b)=> b.count-a.count || a.name.localeCompare(b.name))
  const visible = showAll ? ordered : ordered.filter(x=> selected.includes(x.name)).concat(ordered.filter(x=> !selected.includes(x.name))).slice(0,20)

  return (
    <div className="tag-filter">
      <div className="tag-filter-head">
        <span>Filter by tag</span>
        {selected.length>1 && (
          <label className="inline-check">
            <input type="checkbox" checked={match==='all'} onChange={e=> onMatchChange(e.target.checked ? 'all' : 'any')} />
            Match all selected
          </label>
        )}
        {selected.length>0 && <button type="button" onClick={onClear}>Clear tags</button>}
        {onRefreshAll && (
          <button type="button" onClick={onRefreshAll} disabled={!!refreshing} style={{marginLeft:'auto'}}
                  title="Re-fetch genres from OpenLibrary for every book listed below">
            {refreshing || 'Refresh all tags'}
          </button>
        )}
      </div>
      <div className="tag-cloud">
        {visible.map(tag=> (
          <button key={tag.name} type="button"
                  className={selected.includes(tag.name) ? 'tag selectable selected' : 'tag selectable'}
                  onClick={()=>onToggle(tag.name)}>
            {tag.name} <span className="tag-count">{tag.count}</span>
          </button>
        ))}
        {ordered.length>visible.length && <button type="button" className="tag selectable" onClick={()=>setShowAll(true)}>+{ordered.length-visible.length} more</button>}
        {showAll && <button type="button" className="tag selectable" onClick={()=>setShowAll(false)}>Show fewer</button>}
      </div>
    </div>
  )
}

const TAGS_SHOWN = 3

function TagsCell({book, onChanged, onError}){
  const [busy,setBusy]=useState(false)
  const [showAll,setShowAll]=useState(false)
  const tags = book.tags || []
  const hidden = Math.max(0, tags.length - TAGS_SHOWN)
  const visible = showAll ? tags : tags.slice(0, TAGS_SHOWN)

  const lookup = async ()=>{
    // Refreshing replaces the tags so stale ones are cleaned out; the first
    // lookup for an untagged book just adds them.
    const replace = tags.length > 0
    if(replace && !confirm(`Re-fetch tags from OpenLibrary for "${book.title}"?\n\nIts current tags, including any you added by hand, will be replaced.`)) return
    const url = `${API_BASE}/books/${book.id}/tags/lookup${replace ? '?replace=true' : ''}`
    setBusy(true); onError(null)
    try{
      const res = await fetch(url, {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else if(onChanged) onChanged(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  return (
    <div className="tags-cell">
      {tags.length>0
        ? <div className="tag-list">
            {visible.map(tag=> <span key={tag} className="tag">{tag}</span>)}
            {hidden>0 && (
              <button type="button" className="tag more" onClick={()=>setShowAll(!showAll)}>
                {showAll ? 'show less' : `… +${hidden}`}
              </button>
            )}
          </div>
        : <span className="muted">No tags</span>}
      <button type="button" onClick={lookup} disabled={busy}
              title="Fetch genres from OpenLibrary or Google Books">
        {busy ? '...' : (tags.length ? 'Refresh tags' : 'Lookup tags')}
      </button>
    </div>
  )
}

// --- shelves ---

/// A clickable shelf. Used for placing a book, and for showing where one lives.
/// Pass onDropBook to accept books dragged onto a slot.
function ShelfGrid({shelf, slots, selected, onSelect, highlight, excludeBookId, onDropBook, dragActive}){
  const [hover,setHover] = useState(null)
  if(!shelf) return null
  const occupied = {}
  ;(slots||[]).forEach(s=>{
    const key = `${s.column},${s.row}`
    ;(occupied[key] = occupied[key] || []).push(s)
  })

  const cells = []
  for(let r=1; r<=shelf.rows; r++){
    for(let c=1; c<=shelf.columns; c++){
      const key = `${c},${r}`
      const here = (occupied[key] || []).filter(s=> s.book_id !== excludeBookId)
      const isSelected = selected && selected.column===c && selected.row===r
      const isHighlight = highlight && highlight.column===c && highlight.row===r
      const names = here.map(s=> s.title).join('\n')
      const dropProps = onDropBook ? {
        // preventDefault on dragover is what actually marks a element as a
        // valid drop target; without it the browser refuses the drop.
        onDragOver: (e)=>{ e.preventDefault(); e.dataTransfer.dropEffect = 'move' },
        onDragEnter: ()=> setHover(key),
        onDragLeave: ()=> setHover(h=> h===key ? null : h),
        onDrop: (e)=>{
          e.preventDefault()
          setHover(null)
          const id = Number(e.dataTransfer.getData('text/plain'))
          if(id) onDropBook(id, c, r)
        }
      } : {}
      cells.push(
        <button key={key} type="button" {...dropProps}
                className={['shelf-slot', here.length? 'occupied':'', isSelected? 'selected':'',
                            isHighlight? 'highlight':'', dragActive? 'droppable':'',
                            hover===key? 'drop-hover':''].filter(Boolean).join(' ')}
                onClick={onSelect? ()=>onSelect(c, r) : undefined}
                disabled={!onSelect && !onDropBook}
                title={here.length? `Column ${c}, row ${r}\n${names}` : `Column ${c}, row ${r} — empty`}>
          <span className="shelf-slot-coord">{c},{r}</span>
          {here.length>0 && <span className="shelf-slot-books">{here.length>1? here.length+' books' : here[0].title}</span>}
        </button>
      )
    }
  }
  return (
    <div className="shelf-grid" style={{gridTemplateColumns:`repeat(${shelf.columns},minmax(0,1fr))`}}>
      {cells}
    </div>
  )
}

/// Modal for placing one book: pick a shelf, click a slot or type the numbers.
function ShelfPicker({book, shelves, onClose, onSaved}){
  const firstId = book.shelf_id || (shelves[0] && shelves[0].id) || null
  const [shelfId,setShelfId] = useState(firstId)
  const [col,setCol] = useState(book.shelf_column || '')
  const [row,setRow] = useState(book.shelf_row || '')
  const [layout,setLayout] = useState(null)
  const [busy,setBusy] = useState(false)
  const [error,setError] = useState(null)

  const shelf = (layout && layout.shelf) || shelves.find(s=> s.id===shelfId)

  useEffect(()=>{
    let cancelled = false
    if(!shelfId){ setLayout(null); return }
    ;(async ()=>{
      try{
        const res = await fetch(`${API_BASE}/shelves/${shelfId}/layout`, {headers: authHeaders()})
        if(res.ok && !cancelled) setLayout(await res.json())
      }catch(err){ if(!cancelled) setError(friendlyMessage(err.message)) }
    })()
    return ()=>{ cancelled = true }
  }, [shelfId])

  const pick = (c, r)=>{ setCol(c); setRow(r); setError(null) }

  // Show what else is already in the chosen slot, so a clash is visible before
  // saving rather than silently allowed.
  const clash = (layout && col && row)
    ? (layout.slots || []).filter(s=> s.column===Number(col) && s.row===Number(row) && s.book_id!==book.id)
    : []

  const submit = async (body)=>{
    setBusy(true); setError(null)
    try{
      const res = await fetch(`${API_BASE}/books/${book.id}/location`, {
        method:'PUT', headers: authHeaders(true), body: JSON.stringify(body)})
      if(!res.ok){ setError(await readError(res)); setBusy(false); return }
      onSaved(await res.json())
      onClose()
    }catch(err){ setError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  const save = ()=> submit((col && row && shelfId)
    ? {shelf_id: shelfId, shelf_column: Number(col), shelf_row: Number(row)}
    : {})

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e=> e.stopPropagation()}>
        <h3 style={{marginTop:0}}>Where is “{book.title}”?</h3>
        {error && <div className="alert">{error}</div>}

        {shelves.length===0 ? (
          <div style={{color:'#666'}}>No shelves yet — add one on the Shelves tab first.</div>
        ) : (
          <>
            <label>Shelf
              <select value={shelfId || ''} onChange={e=>{ setShelfId(Number(e.target.value)); setCol(''); setRow('') }}>
                {shelves.map(s=> <option key={s.id} value={s.id}>{s.name} ({s.columns}×{s.rows})</option>)}
              </select>
            </label>

            <div className="shelf-frame">
              <ShelfGrid shelf={shelf} slots={layout && layout.slots}
                         selected={col && row ? {column:Number(col), row:Number(row)} : null}
                         onSelect={pick} excludeBookId={book.id} />
            </div>

            <div className="search-row" style={{marginTop:10,alignItems:'flex-end'}}>
              <label style={{margin:0,flex:'0 0 auto'}}>Column
                <input type="number" min="1" max={shelf? shelf.columns : 99} value={col}
                       style={{width:90}} onChange={e=> setCol(e.target.value)} />
              </label>
              <label style={{margin:0,flex:'0 0 auto'}}>Row
                <input type="number" min="1" max={shelf? shelf.rows : 99} value={row}
                       style={{width:90}} onChange={e=> setRow(e.target.value)} />
              </label>
            </div>

            {clash.length>0 && (
              <div className="notice">
                Also in this slot: {clash.map(s=> s.title).join(', ')}
              </div>
            )}
          </>
        )}

        <div style={{display:'flex',gap:8,marginTop:14,flexWrap:'wrap'}}>
          <button type="button" className="primary" onClick={save} disabled={busy || !shelves.length}>
            {busy? 'Saving...' : 'Save location'}
          </button>
          {book.shelf_id && <button type="button" onClick={()=> submit({})} disabled={busy}>Remove location</button>}
          <button type="button" onClick={onClose} style={{marginLeft:'auto'}}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

/// A plain cover thumbnail, for lists that do not need the edit controls.
function CoverThumb({book, width=34}){
  const height = Math.round(width * 4 / 3)
  if(!book.has_cover){
    return <div className="cover-thumb cover-thumb-empty" style={{width, height, fontSize:9}}>—</div>
  }
  return <img className="cover-thumb" style={{width, height}} src={coverUrl(book, 0)} alt="" />
}

/// Browse a shelf: see it drawn, click a slot to see what is in it.
function BookshelfBrowser({shelves, refreshToken, onPlace, onDelete, onMoved}){
  const [shelfId,setShelfId] = useState(shelves[0] ? shelves[0].id : null)
  const [books,setBooks] = useState([])
  const [selected,setSelected] = useState(null)
  const [loading,setLoading] = useState(false)
  const [error,setError] = useState(null)
  const [dragging,setDragging] = useState(null)

  // Keep a valid shelf selected as shelves come and go.
  useEffect(()=>{
    if(!shelves.length){ setShelfId(null); return }
    if(!shelves.some(s=> s.id===shelfId)) setShelfId(shelves[0].id)
  }, [shelves])

  // A slot number means nothing once the shelf changes, and could point outside
  // a smaller one.
  useEffect(()=>{ setSelected(null) }, [shelfId])

  useEffect(()=>{
    let cancelled = false
    if(!shelfId){ setBooks([]); return }
    setLoading(true)
    ;(async ()=>{
      try{
        const res = await fetch(`${API_BASE}/books?shelf_id=${shelfId}&sort=location&dir=asc`, {headers: authHeaders()})
        if(!res.ok){ if(!cancelled) setError(await readError(res)); return }
        if(!cancelled){ setBooks(await res.json()); setError(null) }
      }catch(err){ if(!cancelled) setError(friendlyMessage(err.message)) }
      finally{ if(!cancelled) setLoading(false) }
    })()
    return ()=>{ cancelled = true }
  }, [shelfId, refreshToken])

  const shelf = shelves.find(s=> s.id===shelfId)
  // The books themselves carry their position, so occupancy needs no extra call.
  const slots = books
    .filter(b=> b.shelf_column && b.shelf_row)
    .map(b=> ({column: b.shelf_column, row: b.shelf_row, book_id: b.id, title: b.title}))

  const inSlot = selected
    ? books.filter(b=> b.shelf_column===selected.column && b.shelf_row===selected.row)
    : []

  /// Drop handler: move a book to a slot on the shelf being viewed. Works
  /// across shelves too, since hovering a shelf tab mid-drag switches to it.
  const moveBook = async (bookId, column, row)=>{
    const book = books.find(b=> b.id===bookId)
    setDragging(null)
    // A book dragged from another shelf will not be in this list; only skip
    // when we can see it is already exactly here.
    if(book && book.shelf_id===shelfId && book.shelf_column===column && book.shelf_row===row) return
    setError(null)
    try{
      const res = await fetch(`${API_BASE}/books/${bookId}/location`, {
        method:'PUT', headers: authHeaders(true),
        body: JSON.stringify({shelf_id: shelfId, shelf_column: column, shelf_row: row})})
      if(!res.ok){ setError(await readError(res)); return }
      const updated = await res.json()
      setBooks(prev=> prev.some(b=> b.id===updated.id)
        ? prev.map(b=> b.id===updated.id ? updated : b)
        : [...prev, updated])
      // Follow the book, so it is obvious where it landed.
      setSelected({column, row})
      if(onMoved) onMoved(updated)
    }catch(err){ setError(friendlyMessage(err.message)) }
  }

  if(!shelves.length){
    return <div className="card"><h3>Bookshelf</h3>
      <div style={{color:'#666'}}>No shelves yet — add one below to get started.</div></div>
  }

  return (
    <div className="card" style={{minWidth:0}}>
      <div style={{display:'flex',alignItems:'center',gap:10,flexWrap:'wrap'}}>
        <h3 style={{margin:0}}>Bookshelf</h3>
        {shelves.length>1 && (
          <div className="shelf-tabs">
            {shelves.map(s=> (
              <button key={s.id} type="button"
                      className={s.id===shelfId ? 'shelf-tab active' : 'shelf-tab'}
                      onClick={()=>{ setShelfId(s.id); setSelected(null) }}
                      // Hovering a shelf while dragging switches to it, which is
                      // what makes moving a book between shelves possible.
                      onDragOver={e=>{
                        e.preventDefault()
                        if(dragging && s.id!==shelfId){ setShelfId(s.id); setSelected(null) }
                      }}>
                {s.name}
              </button>
            ))}
          </div>
        )}
        <span style={{marginLeft:'auto',color:'#666',fontSize:13}}>
          {loading ? 'Loading...' : `${books.length} book${books.length===1?'':'s'} on ${shelf? shelf.name : 'this shelf'}`}
        </span>
      </div>

      {error && <div className="alert" style={{marginTop:8}}>{error}</div>}

      <div className="shelf-browse">
        <div className="shelf-frame" style={{flex:'1 1 320px'}}>
          <ShelfGrid shelf={shelf} slots={slots}
                     selected={selected}
                     onSelect={(c,r)=> setSelected({column:c, row:r})}
                     onDropBook={moveBook}
                     dragActive={!!dragging} />
        </div>

        <div className="slot-detail">
          {!selected ? (
            <div className="slot-detail-empty">Click a slot to see what is there.</div>
          ) : (
            <>
              <div className="slot-detail-head">
                <strong>Column {selected.column}, row {selected.row}</strong>
                <span>{inSlot.length? `${inSlot.length} book${inSlot.length===1?'':'s'}` : 'Empty'}</span>
              </div>
              {inSlot.length>0 && (
                <div className="drag-hint">
                  Drag a book onto a slot to move it{shelves.length>1 ? ', or onto a shelf name to move it there' : ''}.
                </div>
              )}
              {inSlot.length===0 && <div className="slot-detail-empty">Nothing here yet.</div>}
              {inSlot.map(b=> (
                <div key={b.id}
                     className={dragging===b.id ? 'slot-book dragging' : 'slot-book'}
                     draggable
                     onDragStart={e=>{
                       e.dataTransfer.setData('text/plain', String(b.id))
                       e.dataTransfer.effectAllowed = 'move'
                       setDragging(b.id)
                     }}
                     onDragEnd={()=> setDragging(null)}>
                  <span className="drag-handle" title="Drag onto a slot to move this book">⠿</span>
                  <CoverThumb book={b} width={34} />
                  <div style={{minWidth:0,flex:1}}>
                    <div className="slot-book-title">{b.title}</div>
                    {b.author && <div className="slot-book-author">{b.author}</div>}
                    {b.tags && b.tags.length>0 && (
                      <div className="slot-book-tags">{b.tags.slice(0,3).join(' · ')}</div>
                    )}
                  </div>
                  <div className="nowrap">
                    <button type="button" onClick={()=> onPlace(b)}>Move</button>
                    <button type="button" onClick={()=> onDelete(b)} style={{marginLeft:6}}>Delete</button>
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/// Managing the shelves themselves: add, rename, resize, delete.
function ShelvesPanel({shelves, onChanged}){
  const [error,setError] = useState(null)
  const [busy,setBusy] = useState(false)
  const [editingId,setEditingId] = useState(null)
  const [edit,setEdit] = useState({name:'', columns:6, rows:8})
  const [draft,setDraft] = useState({name:'', columns:6, rows:8})
  const [preview,setPreview] = useState(null)

  const call = async (url, options)=>{
    setBusy(true); setError(null)
    try{
      const res = await fetch(url, options)
      if(!res.ok){ setError(await readError(res)); setBusy(false); return null }
      const body = res.status===200 ? await res.json() : null
      await onChanged()
      setBusy(false)
      return body
    }catch(err){ setError(friendlyMessage(err.message)); setBusy(false); return null }
  }

  const add = async ()=>{
    const name = t(draft.name)
    if(!name){ setError('Give the shelf a name'); return }
    const created = await call(`${API_BASE}/shelves`, {
      method:'POST', headers: authHeaders(true),
      body: JSON.stringify({name, columns: Number(draft.columns), rows: Number(draft.rows)})})
    if(created) setDraft({name:'', columns:6, rows:8})
  }

  const startEdit = (s)=>{ setEditingId(s.id); setEdit({name:s.name, columns:s.columns, rows:s.rows}); setError(null) }

  const saveEdit = async (s)=>{
    const name = t(edit.name)
    if(!name){ setError('Give the shelf a name'); return }
    const saved = await call(`${API_BASE}/shelves/${s.id}`, {
      method:'PUT', headers: authHeaders(true),
      body: JSON.stringify({name, columns: Number(edit.columns), rows: Number(edit.rows), sort_order: s.sort_order})})
    if(saved) setEditingId(null)
  }

  const remove = async (s)=>{
    const warning = s.book_count
      ? `\n\n${s.book_count} book${s.book_count===1?'':'s'} will lose their location. The books themselves are kept.`
      : ''
    if(!confirm(`Delete “${s.name}”?${warning}`)) return
    await call(`${API_BASE}/shelves/${s.id}`, {method:'DELETE', headers: authHeaders()})
  }

  return (
    <div className="card" style={{minWidth:0}}>
      <h3>Shelves</h3>
      {error && <div className="alert">{error}</div>}

      <table className="books">
        <thead><tr><th>Name</th><th>Size</th><th>Books</th><th></th></tr></thead>
        <tbody>
          {shelves.map(s=> (
            <tr key={s.id}>
              {editingId===s.id ? (
                <>
                  <td><input value={edit.name} onChange={e=> setEdit({...edit, name:e.target.value})} /></td>
                  <td className="nowrap">
                    <input type="number" min="1" max="50" value={edit.columns} style={{width:70}}
                           onChange={e=> setEdit({...edit, columns:e.target.value})} />
                    {' × '}
                    <input type="number" min="1" max="50" value={edit.rows} style={{width:70}}
                           onChange={e=> setEdit({...edit, rows:e.target.value})} />
                  </td>
                  <td>{s.book_count}</td>
                  <td className="nowrap">
                    <button onClick={()=> saveEdit(s)} disabled={busy}>Save</button>
                    <button onClick={()=> setEditingId(null)} style={{marginLeft:6}}>Cancel</button>
                  </td>
                </>
              ) : (
                <>
                  <td>{s.name}</td>
                  <td className="nowrap">{s.columns} × {s.rows} <span style={{color:'#999'}}>({s.columns*s.rows} slots)</span></td>
                  <td>{s.book_count}</td>
                  <td className="nowrap">
                    <button onClick={()=> setPreview(preview===s.id ? null : s.id)}>{preview===s.id? 'Hide':'View'}</button>
                    <button onClick={()=> startEdit(s)} style={{marginLeft:6}}>Edit</button>
                    <button onClick={()=> remove(s)} style={{marginLeft:6}} disabled={busy}>Delete</button>
                  </td>
                </>
              )}
            </tr>
          ))}
          {shelves.length===0 && <tr><td colSpan={4} style={{color:'#666'}}>No shelves yet.</td></tr>}
        </tbody>
      </table>

      {preview && <ShelfPreview shelfId={preview} />}

      <h4 style={{marginBottom:6}}>Add a shelf</h4>
      <div className="search-row" style={{alignItems:'flex-end'}}>
        <label style={{margin:0,flex:'1 1 160px'}}>Name
          <input value={draft.name} placeholder="Landing bookcase"
                 onChange={e=> setDraft({...draft, name:e.target.value})} />
        </label>
        <label style={{margin:0,flex:'0 0 auto'}}>Columns
          <input type="number" min="1" max="50" value={draft.columns} style={{width:90}}
                 onChange={e=> setDraft({...draft, columns:e.target.value})} />
        </label>
        <label style={{margin:0,flex:'0 0 auto'}}>Rows
          <input type="number" min="1" max="50" value={draft.rows} style={{width:90}}
                 onChange={e=> setDraft({...draft, rows:e.target.value})} />
        </label>
        <button type="button" className="primary" onClick={add} disabled={busy}>Add shelf</button>
      </div>
      <div style={{marginTop:8,fontSize:12,color:'#666'}}>
        Column 1, row 1 is the top left. Shrinking a shelf is refused while books
        sit in the slots that would be cut off.
      </div>
    </div>
  )
}

/// Read-only look at what is on a shelf.
function ShelfPreview({shelfId}){
  const [layout,setLayout] = useState(null)
  useEffect(()=>{
    let cancelled = false
    ;(async ()=>{
      const res = await fetch(`${API_BASE}/shelves/${shelfId}/layout`, {headers: authHeaders()})
      if(res.ok && !cancelled) setLayout(await res.json())
    })()
    return ()=>{ cancelled = true }
  }, [shelfId])
  if(!layout) return null
  return (
    <div className="shelf-frame" style={{marginTop:10}}>
      <ShelfGrid shelf={layout.shelf} slots={layout.slots} />
    </div>
  )
}

/// The location cell in the books table: shows where a book is, and opens the
/// picker. Editing happens in the modal rather than inline, because a position
/// is three linked values and a free-text cell cannot validate them.
function LocationCell({book, shelves, onPlace}){
  const shelf = (shelves || []).find(s=> s.id === book.shelf_id)
  const placed = book.shelf_id && book.shelf_column && book.shelf_row
  return (
    <button type="button" className={placed? 'location-btn placed' : 'location-btn'}
            onClick={()=> onPlace(book)}
            title={placed? `${shelf? shelf.name : 'Shelf'} — column ${book.shelf_column}, row ${book.shelf_row}`
                         : 'Set where this book lives'}>
      {placed
        ? <>
            <span className="location-shelf">{shelf? shelf.name : 'Shelf'}</span>
            <span className="location-coord">{book.shelf_column},{book.shelf_row}</span>
          </>
        : <span className="location-empty">Place</span>}
    </button>
  )
}

function SourcesCell({book, onChanged, onError}){
  const [busy,setBusy]=useState(null)

  const lookup = async (kind)=>{
    setBusy(kind); onError(null)
    try{
      const res = await fetch(`${API_BASE}/books/${book.id}/${kind}/lookup`, {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else if(onChanged) onChanged(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(null)
  }

  return (
    <div className="sources-cell">
      <div className="source-row">
        <span className="source-label">OL</span>
        {book.olid
          ? <a className="source-link" href={`https://openlibrary.org/books/${book.olid}`} target="_blank" rel="noreferrer" title={book.olid}>{book.olid}</a>
          : <button type="button" onClick={()=>lookup('olid')} disabled={!!busy || !book.isbn}
                    title={book.isbn ? 'Look up the OpenLibrary edition id from the ISBN' : 'Add an ISBN first'}>
              {busy==='olid' ? '...' : 'Lookup'}
            </button>}
      </div>
      <div className="source-row">
        <span className="source-label">GB</span>
        {book.google_id
          ? <a className="source-link" href={`https://books.google.com/books?id=${book.google_id}`} target="_blank" rel="noreferrer" title={book.google_id}>{book.google_id}</a>
          : <button type="button" onClick={()=>lookup('google')} disabled={!!busy || (!book.isbn && !book.title)}
                    title="Look up the Google Books volume id">
              {busy==='google' ? '...' : 'Lookup'}
            </button>}
      </div>
    </div>
  )
}

function SortHeader({label, field, sort, onSort}){
  if(!onSort) return <th>{label}</th>
  const active = sort && sort.field === field
  return (
    <th className="sortable">
      <button type="button" className={active? 'sort-btn active':'sort-btn'} onClick={()=>onSort(field)}
              aria-label={`Sort by ${label}`}>
        {label}<span className="sort-arrow">{active ? (sort.dir==='asc' ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>
  )
}

function BooksTable({books, onDelete, onSaved, onBookPatched, emptyText, sort, onSort, shelves, onPlace}){
  const [editingId, setEditingId] = useState(null)
  const [editVals, setEditVals] = useState({title:'', author:'', isbn:'', olid:'', googleId:'', notes:'', tags:'', added:'', addedOriginal:''})
  const [rowError, setRowError] = useState(null)

  const startEdit = (b)=>{
    setRowError(null)
    setEditingId(b.id)
    const added = toDateInput(b.created_at)
    setEditVals({title: b.title||'', author: b.author||'', isbn: b.isbn||'', olid: b.olid||'', googleId: b.google_id||'', notes: b.notes||'', tags: (b.tags||[]).join(', '), added, addedOriginal: added})
  }
  const cancelEdit = ()=>{ setEditingId(null); setRowError(null); setEditVals({title:'', author:'', isbn:'', olid:'', googleId:'', notes:'', tags:'', added:'', addedOriginal:''}) }

  const saveEdit = async (book)=>{
    setRowError(null)
    const vals = {title: t(editVals.title), author: t(editVals.author), isbn: t(editVals.isbn), olid: t(editVals.olid),
                  google_id: t(editVals.googleId), notes: t(editVals.notes),
                  tags: editVals.tags.split(',').map(t).filter(Boolean)}
    if(!vals.title){ setRowError('Title is required'); return }
    if(vals.isbn && !validateISBN(vals.isbn)){ setRowError('ISBN must be 10 or 13 digits'); return }
    if(vals.olid && !/^OL\d+M$/i.test(vals.olid)){ setRowError('OLID must look like OL12345M'); return }
    if(vals.google_id && !/^[A-Za-z0-9_-]{8,40}$/.test(vals.google_id)){ setRowError('Google ID must look like otCEEQAAQBAJ'); return }
    // Only send the added date when it was actually changed, so an untouched row
    // keeps the time-of-day part of its original timestamp.
    if(editVals.added !== editVals.addedOriginal){
      if(!editVals.added){ setRowError('Date added cannot be cleared'); return }
      vals.created_at = editVals.added
    }
    try{
      const res = await fetch(API_BASE + '/books/' + book.id, {method: 'PUT', headers: authHeaders(true), body: JSON.stringify(vals)})
      if(!res.ok){ setRowError(await readError(res)); return }
      const updated = await res.json()
      cancelEdit()
      if(onSaved) onSaved(updated, book)
    }catch(err){ setRowError(friendlyMessage(err.message)) }
  }

  if(!books || books.length===0){
    return <div style={{padding:12,color:'#666'}}>{emptyText || 'No books yet.'}</div>
  }

  return (
    <div style={{overflowX:'auto',maxWidth:'100%'}}>
      {rowError && <div className="alert">{rowError}</div>}
      <table className="books">
        <thead><tr>
          <th>Cover</th>
          <SortHeader label="Title" field="title" sort={sort} onSort={onSort} />
          <SortHeader label="Author" field="author" sort={sort} onSort={onSort} />
          <th>ISBN</th>
          <th>Sources</th>
          <SortHeader label="Location" field="location" sort={sort} onSort={onSort} />
          <th>Tags</th>
          <th className="col-notes">Notes</th>
          <SortHeader label="Added" field="added" sort={sort} onSort={onSort} />
          <th></th>
        </tr></thead>
        <tbody>
          {books.map(b=> (
            <tr key={b.id}>
              <td><CoverCell book={b} onChanged={onBookPatched} onError={setRowError} /></td>
              {editingId===b.id ? (
                <>
                  <td><input value={editVals.title} onChange={e=>setEditVals({...editVals, title: e.target.value})} /></td>
                  <td><input value={editVals.author} onChange={e=>setEditVals({...editVals, author: e.target.value})} /></td>
                  <td><input value={editVals.isbn} onChange={e=>setEditVals({...editVals, isbn: e.target.value})} /></td>
                  <td>
                    <input value={editVals.olid} placeholder="OL12345M" onChange={e=>setEditVals({...editVals, olid: e.target.value})} />
                    <input style={{marginTop:4}} value={editVals.googleId} placeholder="otCEEQAAQBAJ" onChange={e=>setEditVals({...editVals, googleId: e.target.value})} />
                  </td>
                  <td className="nowrap"><LocationCell book={b} shelves={shelves} onPlace={onPlace} /></td>
                  <td><input value={editVals.tags} placeholder="comma, separated, tags" onChange={e=>setEditVals({...editVals, tags: e.target.value})} /></td>
                  <td className="col-notes"><input value={editVals.notes} onChange={e=>setEditVals({...editVals, notes: e.target.value})} /></td>
                  <td><input type="date" value={editVals.added} onChange={e=>setEditVals({...editVals, added: e.target.value})} /></td>
                  <td className="nowrap">
                    <button onClick={()=>saveEdit(b)}>Save</button>
                    <button onClick={cancelEdit} style={{marginLeft:6}}>Cancel</button>
                  </td>
                </>
              ) : (
                <>
                  <td className="col-title">{b.title}</td>
                  <td className="col-author">{b.author}</td>
                  <td>{b.isbn}</td>
                  <td><SourcesCell book={b} onChanged={onBookPatched} onError={setRowError} /></td>
                  <td className="nowrap"><LocationCell book={b} shelves={shelves} onPlace={onPlace} /></td>
                  <td><TagsCell book={b} onChanged={onBookPatched} onError={setRowError} /></td>
                  <td className="col-notes">{b.notes}</td>
                  <td className="nowrap">{formatAdded(b.created_at)}</td>
                  <td className="nowrap">
                    <button onClick={()=>startEdit(b)}>Edit</button>
                    <button onClick={()=>onDelete(b)} style={{marginLeft:6}}>Delete</button>
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function App(){
  const [tab,setTab]=useState('add')
  const [books,setBooks]=useState([])
  const [recent,setRecent]=useState([])
  const [q,setQ]=useState('')
  const [loggedIn,setLoggedIn]=useState(!!localStorage.getItem('token'))
  const [undo,setUndo]=useState(null)
  const [undoTimer,setUndoTimer]=useState(null)
  const [sort,setSort]=useState({field:'added', dir:'desc'})
  const [allTags,setAllTags]=useState([])
  const [selectedTags,setSelectedTags]=useState([])
  const [tagMatch,setTagMatch]=useState('any')
  const [refreshing,setRefreshing]=useState(null)
  const [shelves,setShelves]=useState([])
  const [placing,setPlacing]=useState(null)
  // Bumped whenever a location changes, so the bookshelf browser refetches.
  const [locationVersion,setLocationVersion]=useState(0)

  const sortNewestFirst = (list)=> Array.isArray(list) ? [...list].sort((a,b)=> (b.id||0)-(a.id||0)) : []

  const fetchShelves = async ()=>{
    const res = await fetch(API_BASE + '/shelves', {headers: authHeaders()})
    if(res.ok) setShelves(await res.json())
  }

  const fetchTags = async ()=>{
    const res = await fetch(API_BASE + '/tags', {headers: authHeaders()})
    if(res.ok) setAllTags(await res.json())
  }

  const fetchBooks = async (query, sortOverride, filterOverride)=>{
    const term = t(query===undefined ? q : query)
    const s = sortOverride || sort
    const f = filterOverride || {tags: selectedTags, match: tagMatch}
    const params = [`sort=${s.field}`, `dir=${s.dir}`]
    if(term) params.push('q=' + encodeURIComponent(term))
    if(f.tags && f.tags.length){
      params.push('tags=' + encodeURIComponent(f.tags.join(',')))
      params.push('match=' + f.match)
    }
    const res = await fetch(API_BASE + '/books?' + params.join('&'), {headers: authHeaders()})
    if(res.status===401){ setLoggedIn(false); return }
    // The API already applied the ordering, so keep the response order as-is.
    setBooks(await res.json())
  }

  const toggleSort = (field)=>{
    const next = sort.field===field
      ? {field, dir: sort.dir==='asc' ? 'desc' : 'asc'}
      : {field, dir: field==='added' ? 'desc' : 'asc'}
    setSort(next)
    fetchBooks(undefined, next)
  }

  const toggleTag = (name)=>{
    const next = selectedTags.includes(name) ? selectedTags.filter(x=> x!==name) : [...selectedTags, name]
    setSelectedTags(next)
    fetchBooks(undefined, undefined, {tags: next, match: tagMatch})
  }

  const changeTagMatch = (mode)=>{
    setTagMatch(mode)
    if(selectedTags.length) fetchBooks(undefined, undefined, {tags: selectedTags, match: mode})
  }

  const clearTagFilter = ()=>{
    setSelectedTags([])
    fetchBooks(undefined, undefined, {tags: [], match: tagMatch})
  }

  // Re-fetch genres for every listed book, one at a time so we stay polite to
  // OpenLibrary. Used to clean up tags stored by an older tag lookup.
  const refreshAllTags = async ()=>{
    const targets = books.filter(b=> b.isbn || b.olid)
    if(!targets.length) return
    if(!confirm(`Re-fetch tags from OpenLibrary for ${targets.length} book${targets.length===1?'':'s'}?\n\nExisting tags on those books, including any you added by hand, will be replaced.`)) return
    for(let i=0;i<targets.length;i++){
      setRefreshing(`Refreshing ${i+1}/${targets.length}...`)
      try{
        const res = await fetch(`${API_BASE}/books/${targets[i].id}/tags/lookup?replace=true`, {method:'POST', headers: authHeaders()})
        if(res.ok){
          const updated = await res.json()
          setBooks(prev=> prev.map(x=> x.id===updated.id ? {...x, ...updated} : x))
        }
      }catch(e){ console.error('tag refresh failed', e) }
    }
    setRefreshing(null)
    fetchTags()
  }

  useEffect(()=>{ if(loggedIn){ fetchBooks(); fetchTags(); fetchShelves() } }, [loggedIn])

  const setUndoWithTimeout = (u)=>{
    if(undoTimer) clearTimeout(undoTimer)
    setUndo(u)
    setUndoTimer(setTimeout(()=> setUndo(null), 8000))
  }

  const clearUndo = ()=>{
    setUndo(null)
    if(undoTimer){ clearTimeout(undoTimer); setUndoTimer(null) }
  }

  const performUndo = async ()=>{
    if(!undo) return
    try{
      if(undo.type==='delete'){
        const res = await fetch(API_BASE + '/books', {method:'POST', headers: authHeaders(true), body: JSON.stringify({title: undo.book.title, author: undo.book.author, isbn: undo.book.isbn, olid: undo.book.olid, google_id: undo.book.google_id, notes: undo.book.notes, created_at: undo.book.created_at, tags: undo.book.tags, shelf_id: undo.book.shelf_id, shelf_column: undo.book.shelf_column, shelf_row: undo.book.shelf_row})})
        if(res.ok){
          const restored = await res.json()
          if(undo.wasRecent) setRecent(prev=> sortNewestFirst([restored, ...prev]))
        }
      } else if(undo.type==='update'){
        const res = await fetch(API_BASE + '/books/' + undo.book.id, {method:'PUT', headers: authHeaders(true), body: JSON.stringify({title: undo.book.title, author: undo.book.author, isbn: undo.book.isbn, olid: undo.book.olid, google_id: undo.book.google_id, notes: undo.book.notes, created_at: undo.book.created_at, tags: undo.book.tags, shelf_id: undo.book.shelf_id, shelf_column: undo.book.shelf_column, shelf_row: undo.book.shelf_row})})
        if(res.ok){
          const reverted = await res.json()
          setRecent(prev=> prev.map(x=> x.id===reverted.id ? reverted : x))
        }
      } else if(undo.type==='add'){
        if(undo.book && undo.book.id){
          await fetch(API_BASE + '/books/' + undo.book.id, {method:'DELETE', headers: authHeaders()})
          setRecent(prev=> prev.filter(x=> x.id!==undo.book.id))
        }
      }
    }catch(e){ console.error('Undo failed', e) }
    clearUndo()
    fetchBooks()
    fetchShelves()
    setLocationVersion(v=> v+1)
  }

  const handleDelete = async (book)=>{
    if(!confirm(`Delete "${book.title}"?`)) return
    const wasRecent = recent.some(x=> x.id===book.id)
    await fetch(API_BASE + '/books/' + book.id, {method:'DELETE', headers: authHeaders()})
    setBooks(prev=> prev.filter(x=> x.id!==book.id))
    setRecent(prev=> prev.filter(x=> x.id!==book.id))
    setUndoWithTimeout({type:'delete', book, wasRecent})
    fetchBooks()
    fetchTags()
    fetchShelves()
    setLocationVersion(v=> v+1)
  }

  const onAdded = (created)=>{
    setUndoWithTimeout({type:'add', book: created})
    setRecent(prev=> [created, ...prev.filter(x=> x.id!==created.id)])
    setBooks(prev=> [created, ...prev.filter(x=> x.id!==created.id)])
    fetchTags()
  }

  const onSaved = (updated, previous)=>{
    setUndoWithTimeout({type:'update', book: previous})
    setBooks(prev=> prev.map(x=> x.id===updated.id ? updated : x))
    setRecent(prev=> prev.map(x=> x.id===updated.id ? updated : x))
    fetchTags()
  }

  // Cover, OLID and tag lookups are applied straight away — not part of the undo stack.
  const onBookPatched = (updated)=>{
    setBooks(prev=> prev.map(x=> x.id===updated.id ? {...x, ...updated} : x))
    setRecent(prev=> prev.map(x=> x.id===updated.id ? {...x, ...updated} : x))
    fetchTags()
  }

  const logout = ()=>{ localStorage.removeItem('token'); setLoggedIn(false); setBooks([]); setRecent([]); setAllTags([]); setSelectedTags([]) }

  return (
    <div className={loggedIn ? 'container wide' : 'container'}>
      <div className="header">
        <h1>Book Library</h1>
        {loggedIn && <button onClick={logout}>Logout</button>}
      </div>

      {undo && (
        <div className="snackbar">
          <div>{undo.type==='delete' ? 'Book deleted' : undo.type==='update' ? 'Change saved' : 'Book added'}</div>
          <button onClick={performUndo}>Undo</button>
        </div>
      )}

      {!loggedIn ? <Login onLogin={()=>setLoggedIn(true)} /> : (
        <>
          {placing && (
            <ShelfPicker book={placing} shelves={shelves}
                         onClose={()=> setPlacing(null)}
                         onSaved={(updated)=>{ onBookPatched(updated); fetchShelves(); setLocationVersion(v=> v+1) }} />
          )}
          <div className="tabs">
            <button className={tab==='add'? 'tab active':'tab'} onClick={()=>setTab('add')}>Add</button>
            <button className={tab==='manage'? 'tab active':'tab'} onClick={()=>{ setTab('manage'); fetchBooks(); fetchTags(); fetchShelves() }}>Manage</button>
            <button className={tab==='shelves'? 'tab active':'tab'} onClick={()=>{ setTab('shelves'); fetchShelves() }}>Bookshelf</button>
          </div>

          {tab==='shelves' ? (
            <>
              <BookshelfBrowser shelves={shelves} refreshToken={locationVersion}
                                onPlace={setPlacing} onDelete={handleDelete}
                                onMoved={(updated)=>{ onBookPatched(updated); fetchShelves() }} />
              <ShelvesPanel shelves={shelves} onChanged={async ()=>{ await fetchShelves(); await fetchBooks(); setLocationVersion(v=> v+1) }} />
            </>
          ) : tab==='add' ? (
            <>
              <AddForm onAdded={onAdded} />
              <div className="card" style={{minWidth:0}}>
                <h3>Recently added this session</h3>
                <BooksTable books={recent} onDelete={handleDelete} onSaved={onSaved} onBookPatched={onBookPatched} shelves={shelves} onPlace={setPlacing} emptyText="Nothing added yet — books you add will appear here so you can edit them." />
              </div>
            </>
          ) : (
            <div className="card" style={{minWidth:0}}>
              <h3>Manage library</h3>
              <form className="search-row" onSubmit={e=>{ e.preventDefault(); fetchBooks() }}>
                <input placeholder="Search title, author or ISBN" value={q} onChange={e=>setQ(e.target.value)} />
                <button type="submit">Search</button>
                <button type="button" onClick={()=>{ setQ(''); fetchBooks('') }}>Clear</button>
              </form>
              <TagFilter tags={allTags} selected={selectedTags} match={tagMatch}
                         onToggle={toggleTag} onMatchChange={changeTagMatch} onClear={clearTagFilter}
                         onRefreshAll={books.length ? refreshAllTags : null} refreshing={refreshing} />
              <div style={{margin:'8px 0',color:'#666',fontSize:13}}>{books.length} book{books.length===1?'':'s'}</div>
              <BooksTable books={books} onDelete={handleDelete} onSaved={onSaved} onBookPatched={onBookPatched} sort={sort} onSort={toggleSort} shelves={shelves} onPlace={setPlacing} emptyText="No books match." />
            </div>
          )}
        </>
      )}
    </div>
  )
}
