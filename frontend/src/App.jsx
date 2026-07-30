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
    const vals = {title: t(title), author: t(author), isbn: t(isbn), olid: t(olid), notes: t(notes)}
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
      setTitle(''); setAuthor(''); setIsbn(''); setOlid(''); setNotes('')
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
      if(j.title) setTitle(j.title)
      if(j.authors && j.authors.length) setAuthor(j.authors.join(', '))
      if(j.olid) setOlid(j.olid)
      if(!j.title && (!j.authors || !j.authors.length) && !j.olid) setError('No data found')
    }catch(err){ setError(err.message) }
    setLoading(false)
  }

  const searchMeta = async (includeAll)=>{
    const titleVal = t(title), authorVal = t(author)
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
      if(j.length===0) setError('No matching books found on OpenLibrary')
    }catch(err){ setError(err.message) }
    setSearching(false)
  }

  const handleAddFromSearch = async (doc, isbnVal, olidVal, coverVal) =>{
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

    const payload = { title: titleVal, author: authorsVal, isbn: t(isbnVal || (details && details.isbns && details.isbns[0])), olid: olidVal || null, notes: '', cover_url: coverVal || null }
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
          <button type="submit" className="primary" disabled={searching}>{searching? 'Searching...':'Search editions'}</button>
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
                <div style={{fontSize:13,color:'#666'}}>Editions:</div>
                {(doc.editions && doc.editions.length>0) ? (
                  <>
                    <div className="edition-scroller">
                      {((expandedSet[idx]) ? doc.editions : doc.editions.slice(0,6)).map((ed,ii)=> {
                        const wanted = t(isbn).replace(/[-\s]/g,'')
                        const match = wanted && (ed.isbns||[]).some(x=> String(x).replace(/[-\s]/g,'')===wanted)
                        return (
                          <button key={ed.olid || ii} type="button" className={match? 'edition-card match':'edition-card'} onClick={()=>handleAddFromSearch(doc, (ed.isbns && ed.isbns[0]) || null, ed.olid, ed.cover)}>
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
      <button type="button" onClick={lookup} disabled={busy || (!book.isbn && !book.olid)}
              title={(book.isbn || book.olid) ? 'Fetch genres from OpenLibrary' : 'Add an ISBN or OLID first'}>
        {busy ? '...' : (tags.length ? 'Refresh tags' : 'Lookup tags')}
      </button>
    </div>
  )
}

function OlidCell({book, onChanged, onError}){
  const [busy,setBusy]=useState(false)

  const lookup = async ()=>{
    setBusy(true); onError(null)
    try{
      const res = await fetch(API_BASE + '/books/' + book.id + '/olid/lookup', {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else if(onChanged) onChanged(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  if(book.olid){
    return <a className="olid-link" href={`https://openlibrary.org/books/${book.olid}`} target="_blank" rel="noreferrer">{book.olid}</a>
  }
  return (
    <button type="button" onClick={lookup} disabled={busy || !book.isbn}
            title={book.isbn ? 'Look up the OpenLibrary edition id from the ISBN' : 'Add an ISBN first'}>
      {busy ? '...' : 'Lookup'}
    </button>
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

function BooksTable({books, onDelete, onSaved, onBookPatched, emptyText, sort, onSort}){
  const [editingId, setEditingId] = useState(null)
  const [editVals, setEditVals] = useState({title:'', author:'', isbn:'', olid:'', notes:'', tags:'', added:'', addedOriginal:''})
  const [rowError, setRowError] = useState(null)

  const startEdit = (b)=>{
    setRowError(null)
    setEditingId(b.id)
    const added = toDateInput(b.created_at)
    setEditVals({title: b.title||'', author: b.author||'', isbn: b.isbn||'', olid: b.olid||'', notes: b.notes||'', tags: (b.tags||[]).join(', '), added, addedOriginal: added})
  }
  const cancelEdit = ()=>{ setEditingId(null); setRowError(null); setEditVals({title:'', author:'', isbn:'', olid:'', notes:'', tags:'', added:'', addedOriginal:''}) }

  const saveEdit = async (book)=>{
    setRowError(null)
    const vals = {title: t(editVals.title), author: t(editVals.author), isbn: t(editVals.isbn), olid: t(editVals.olid), notes: t(editVals.notes),
                  tags: editVals.tags.split(',').map(t).filter(Boolean)}
    if(!vals.title){ setRowError('Title is required'); return }
    if(vals.isbn && !validateISBN(vals.isbn)){ setRowError('ISBN must be 10 or 13 digits'); return }
    if(vals.olid && !/^OL\d+M$/i.test(vals.olid)){ setRowError('OLID must look like OL12345M'); return }
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
          <th>OLID</th>
          <th>Tags</th>
          <th>Notes</th>
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
                  <td><input value={editVals.olid} placeholder="OL12345M" onChange={e=>setEditVals({...editVals, olid: e.target.value})} /></td>
                  <td><input value={editVals.tags} placeholder="comma, separated, tags" onChange={e=>setEditVals({...editVals, tags: e.target.value})} /></td>
                  <td><input value={editVals.notes} onChange={e=>setEditVals({...editVals, notes: e.target.value})} /></td>
                  <td><input type="date" value={editVals.added} onChange={e=>setEditVals({...editVals, added: e.target.value})} /></td>
                  <td className="nowrap">
                    <button onClick={()=>saveEdit(b)}>Save</button>
                    <button onClick={cancelEdit} style={{marginLeft:6}}>Cancel</button>
                  </td>
                </>
              ) : (
                <>
                  <td>{b.title}</td>
                  <td>{b.author}</td>
                  <td>{b.isbn}</td>
                  <td className="nowrap"><OlidCell book={b} onChanged={onBookPatched} onError={setRowError} /></td>
                  <td><TagsCell book={b} onChanged={onBookPatched} onError={setRowError} /></td>
                  <td>{b.notes}</td>
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

  const sortNewestFirst = (list)=> Array.isArray(list) ? [...list].sort((a,b)=> (b.id||0)-(a.id||0)) : []

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

  useEffect(()=>{ if(loggedIn){ fetchBooks(); fetchTags() } }, [loggedIn])

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
        const res = await fetch(API_BASE + '/books', {method:'POST', headers: authHeaders(true), body: JSON.stringify({title: undo.book.title, author: undo.book.author, isbn: undo.book.isbn, olid: undo.book.olid, notes: undo.book.notes, created_at: undo.book.created_at, tags: undo.book.tags})})
        if(res.ok){
          const restored = await res.json()
          if(undo.wasRecent) setRecent(prev=> sortNewestFirst([restored, ...prev]))
        }
      } else if(undo.type==='update'){
        const res = await fetch(API_BASE + '/books/' + undo.book.id, {method:'PUT', headers: authHeaders(true), body: JSON.stringify({title: undo.book.title, author: undo.book.author, isbn: undo.book.isbn, olid: undo.book.olid, notes: undo.book.notes, created_at: undo.book.created_at, tags: undo.book.tags})})
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
    <div className="container">
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
          <div className="tabs">
            <button className={tab==='add'? 'tab active':'tab'} onClick={()=>setTab('add')}>Add</button>
            <button className={tab==='manage'? 'tab active':'tab'} onClick={()=>{ setTab('manage'); fetchBooks(); fetchTags() }}>Manage</button>
          </div>

          {tab==='add' ? (
            <>
              <AddForm onAdded={onAdded} />
              <div className="card" style={{minWidth:0}}>
                <h3>Recently added this session</h3>
                <BooksTable books={recent} onDelete={handleDelete} onSaved={onSaved} onBookPatched={onBookPatched} emptyText="Nothing added yet — books you add will appear here so you can edit them." />
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
              <BooksTable books={books} onDelete={handleDelete} onSaved={onSaved} onBookPatched={onBookPatched} sort={sort} onSort={toggleSort} emptyText="No books match." />
            </div>
          )}
        </>
      )}
    </div>
  )
}
