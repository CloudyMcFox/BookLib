import React, { Fragment, createContext, useContext, useState, useEffect, useRef } from 'react'

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

// Ids arrive however they were copied: bare, or as the address bar of the page
// they were read off. The backend already accepts both, so a form that rejects
// what the API would have taken is the form being wrong.
function normalizeOlid(value){
  const m = t(value).toUpperCase().match(/OL\d+M/)
  return m ? m[0] : null
}

function olidProblem(value){
  return /OL\d+W/i.test(t(value))
    ? 'That is a work id. Use the edition id from the book\'s own OpenLibrary page — it ends in M, like OL12345M.'
    : 'OLID must look like OL12345M'
}

function normalizeGoogleId(value){
  const v = t(value)
  if(!v) return null
  const query = v.match(/[?&]id=([A-Za-z0-9_-]+)/)
  if(query) return query[1]
  const bare = v.includes('/') ? (v.split('?')[0].split('/').filter(Boolean).pop() || v) : v
  return /^[A-Za-z0-9_-]{8,40}$/.test(bare) ? bare : null
}

function formatAdded(value){
  if(!value) return '—'
  const d = new Date(value)
  if(isNaN(d.getTime())) return value
  // Timestamps are stored in UTC; render them in UTC so the displayed day always
  // matches the value shown in the date editor.
  return d.toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric', timeZone:'UTC'})
}

function formatCheckout(value){
  if(!value) return ''
  const d = new Date(value)
  if(isNaN(d.getTime())) return value
  return d.toLocaleString(undefined, {year:'numeric', month:'short', day:'numeric', hour:'numeric', minute:'2-digit'})
}

// Stored apart so a series can be sorted in reading order, but read as one
// thing: "The Stormlight Archive (2)".
function seriesLabel(book){
  const name = t(book && book.series)
  if(!name) return ''
  const index = book.series_index
  if(index===null || index===undefined || index==='') return name
  const n = Number(index)
  if(isNaN(n)) return name
  // Number() already drops a trailing .0, so book 3 reads as "(3)" and a novella
  // between two volumes reads as "(3.5)".
  return `${name} (${n})`
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

async function checkoutBook(book){
  const entered = prompt(`Who is checking out "${book.title}"?`)
  if(entered===null) return null
  const borrowerName = t(entered)
  if(!borrowerName) throw new Error('Enter the borrower name')
  const res = await fetch(`${API_BASE}/books/${book.id}/checkout`, {
    method:'POST', headers:authHeaders(true),
    body:JSON.stringify({borrower_name: borrowerName})})
  if(!res.ok) throw new Error(await readError(res))
  return await res.json()
}

async function checkinBook(book){
  const res = await fetch(`${API_BASE}/books/${book.id}/checkin`, {
    method:'POST', headers:authHeaders()})
  if(!res.ok) throw new Error(await readError(res))
  return await res.json()
}

// Guests get a token like anyone else. They cannot edit the catalogue or check
// books in, but the dedicated checkout endpoint lets them borrow books.
const ReadOnlyContext = createContext(false)
const useReadOnly = ()=> useContext(ReadOnlyContext)

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
  const [guestAllowed,setGuestAllowed]=useState(false)
  const [busy,setBusy]=useState(false)

  useEffect(()=>{
    let cancelled = false
    ;(async ()=>{
      try{
        const res = await fetch(API_BASE + '/auth/config')
        if(!res.ok) return
        const cfg = await res.json()
        if(!cancelled) setGuestAllowed(!!cfg.guest_access_enabled)
      }catch(_){ /* leave the guest button hidden */ }
    })()
    return ()=>{ cancelled = true }
  }, [])

  const submit=async e=>{
    e.preventDefault()
    setError(null)
    const body=new URLSearchParams(); body.append('username',t(username)); body.append('password',password); body.append('grant_type','')
    const res = await fetch(API_BASE + '/token',{method:'POST', body})
    if(!res.ok){ setError('Login failed - check username and password'); return }
    const j = await res.json(); localStorage.setItem('token', j.access_token); onLogin();
  }

  const guest=async ()=>{
    setError(null); setBusy(true)
    try{
      const res = await fetch(API_BASE + '/token/guest',{method:'POST'})
      if(!res.ok){ setError(await readError(res)); return }
      const j = await res.json()
      localStorage.setItem('token', j.access_token)
      onLogin()
    }catch(err){ setError(friendlyMessage(err.message)) }
    finally{ setBusy(false) }
  }

  return (
    <form onSubmit={submit} className="card">
      <h3>Login</h3>
      {error && <div className="alert">{error}</div>}
      <label>Username<input value={username} onChange={e=>setUsername(e.target.value)}/></label>
      <label>Password<input type="password" value={password} onChange={e=>setPassword(e.target.value)}/></label>
      <button type="submit">Login</button>
      {guestAllowed && (
        <div className="guest-login">
          <button type="button" onClick={guest} disabled={busy}>
            {busy ? 'Opening...' : 'Browse as guest'}
          </button>
          <span className="muted">Browse the library and check books out. Guests cannot edit the catalogue or check books in.</span>
        </div>
      )}
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
  const [format,setFormat]=useState('')
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
    // Left blank, the server looks the binding up from the ISBN or OLID; typed,
    // it is taken as the answer.
    const vals = {title: t(title), author: t(author), isbn: t(isbn), olid: t(olid), google_id: t(googleId), notes: t(notes), format: t(format) || null}
    const missing = []
    if(!vals.title) missing.push('Title')
    if(!vals.author) missing.push('Author')
    if(!vals.isbn) missing.push('ISBN')
    if(missing.length){ setError(`${missing.join(', ')} ${missing.length>1?'are':'is'} required to add a book manually`); return }
    if(!validateISBN(vals.isbn)){ setError('ISBN must be 10 or 13 digits'); return }
    if(vals.olid){
      const olid = normalizeOlid(vals.olid)
      if(!olid){ setError(olidProblem(vals.olid)); return }
      vals.olid = olid
    }
    try{
      const res = await fetch(API_BASE + '/books',{method:'POST', headers: authHeaders(true), body: JSON.stringify(vals)})
      if(!res.ok){ setError(await readError(res)); return }
      const created = await res.json()
      setTitle(''); setAuthor(''); setIsbn(''); setOlid(''); setGoogleId(''); setNotes(''); setFormat('')
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

  const handleAddFromSearch = async (doc, isbnVal, olidVal, coverVal, googleVal, formatVal) =>{
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

    const payload = { title: titleVal, author: authorsVal, isbn: t(isbnVal || (details && details.isbns && details.isbns[0])), olid: olidVal || null, google_id: googleVal || (details && details.google_id) || null, notes: '', cover_url: coverVal || null,
                      // The chosen edition already says how it is bound; no reason to make the server go and ask.
                      format: formatVal || (details && details.format) || null }
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
        <label>Format <span className="hint">(optional — looked up from OpenLibrary when left blank)</span>
          <input list="known-formats" value={format} placeholder="Paperback" onChange={e=>setFormat(e.target.value)}/>
        </label>
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
                          <button key={ed.olid || ed.isbns?.[0] || ii} type="button" className={match? 'edition-card match':'edition-card'} onClick={()=>handleAddFromSearch(doc, (ed.isbns && ed.isbns[0]) || null, ed.olid, ed.cover, ed.google_id, ed.format)}>
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
  const readOnly = useReadOnly()
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
        {!readOnly && <>
        {!book.has_cover && <button type="button" onClick={lookup} disabled={busy}>{busy? '...' : 'Lookup'}</button>}
        {book.has_cover && <button type="button" onClick={remove} disabled={busy}>Remove</button>}
        <button type="button" onClick={()=>fileRef.current && fileRef.current.click()} disabled={busy}>Upload</button>
        <button type="button" onClick={fromUrl} disabled={busy}>From URL</button>
        <input ref={fileRef} type="file" accept="image/*" style={{display:'none'}} onChange={e=>upload(e.target.files && e.target.files[0])} />
        </>}
      </div>
    </div>
  )
}

function TagFilter({tags, selected, excluded, match, onToggle, onMatchChange, onClear, onRefreshAll, refreshing}){
  const [showAll,setShowAll] = useState(false)
  const [mode,setMode] = useState('include')
  const [order,setOrder] = useState('count')
  if((!tags || tags.length===0) && !onRefreshAll) return null
  // Most used first, but keep active include/exclude tags visible even when the
  // list is capped.
  const ordered = [...(tags||[])].sort((a,b)=>
    order==='alpha'
      ? a.name.localeCompare(b.name)
      : b.count-a.count || a.name.localeCompare(b.name))
  const active = new Set([...selected, ...excluded])
  const visible = showAll
    ? ordered
    : ordered.filter(x=> active.has(x.name)).concat(ordered.filter(x=> !active.has(x.name))).slice(0,20)

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
        {(selected.length>0 || excluded.length>0) && <button type="button" onClick={onClear}>Clear tags</button>}
        <label className="tag-order">
          Order
          <select value={order} onChange={e=>setOrder(e.target.value)}>
            <option value="count">Most used</option>
            <option value="alpha">Alphabetical (A–Z)</option>
          </select>
        </label>
        {onRefreshAll && (
          <button type="button" onClick={onRefreshAll} disabled={!!refreshing} style={{marginLeft:'auto'}}
                  title="Re-fetch genres from OpenLibrary for every book listed below">
            {refreshing || 'Refresh all tags'}
          </button>
        )}
      </div>
      <div className="tag-filter-mode" role="group" aria-label="Tag filter mode">
        <button type="button" className={mode==='include' ? 'active include' : ''}
                onClick={()=>setMode('include')} aria-pressed={mode==='include'}>
          Include
        </button>
        <button type="button" className={mode==='exclude' ? 'active exclude' : ''}
                onClick={()=>setMode('exclude')} aria-pressed={mode==='exclude'}>
          Exclude
        </button>
        <span>
          {mode==='include'
            ? 'Click tags to require them in the results.'
            : 'Click tags to hide any book that has them.'}
        </span>
      </div>
      <div className="tag-cloud">
        {visible.map(tag=>{
          const included = selected.includes(tag.name)
          const isExcluded = excluded.includes(tag.name)
          return (
            <button key={tag.name} type="button"
                    className={['tag','selectable',included?'selected':'',isExcluded?'excluded':''].filter(Boolean).join(' ')}
                    onClick={()=>onToggle(tag.name, mode)}
                    title={included ? 'Included in results' : isExcluded ? 'Excluded from results' : `Add to ${mode} tags`}>
              {isExcluded && <span aria-hidden="true">− </span>}
              {tag.name} <span className="tag-count">{tag.count}</span>
            </button>
          )
        })}
        {ordered.length>visible.length && <button type="button" className="tag selectable" onClick={()=>setShowAll(true)}>+{ordered.length-visible.length} more</button>}
        {showAll && <button type="button" className="tag selectable" onClick={()=>setShowAll(false)}>Show fewer</button>}
      </div>
    </div>
  )
}

const TAGS_SHOWN = 3

function TagsCell({book, onChanged, onError}){
  const readOnly = useReadOnly()
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
      {!readOnly && (
        <button type="button" onClick={lookup} disabled={busy}
                title="Fetch genres from OpenLibrary or Google Books">
          {busy ? '...' : (tags.length ? 'Refresh tags' : 'Lookup tags')}
        </button>
      )}
    </div>
  )
}

// --- format ---

// Offered in the picker. The field is free text underneath: these are the
// spellings worth agreeing on, not the only allowed answers.
const KNOWN_FORMATS = ['Hardcover', 'Leatherbound', 'Paperback', 'Mass market paperback',
                       'Board book', 'Spiral-bound', 'Library binding', 'Ebook', 'Audiobook']

function FormatCell({book, onChanged, onError}){
  const readOnly = useReadOnly()
  const [busy,setBusy]=useState(false)

  const lookup = async ()=>{
    if(book.format && !confirm(`Re-fetch the format from OpenLibrary for "${book.title}"?\n\nIt is currently ${book.format}.`)) return
    setBusy(true); onError(null)
    try{
      const res = await fetch(`${API_BASE}/books/${book.id}/format/lookup`, {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else if(onChanged) onChanged(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  return (
    <div className="format-cell">
      {book.format ? <span>{book.format}</span> : <span className="muted">No format</span>}
      {!readOnly && (
        <button type="button" onClick={lookup} disabled={busy}
                title="Fetch the binding from OpenLibrary. Google Books does not record it.">
          {busy ? '...' : (book.format ? 'Refresh' : 'Lookup')}
        </button>
      )}
    </div>
  )
}

// --- series and description ---

function SeriesCell({book, onChanged, onError}){
  const readOnly = useReadOnly()
  const [busy,setBusy]=useState(false)
  const label = seriesLabel(book)

  const lookup = async ()=>{
    if(label && !confirm(`Re-fetch the series for "${book.title}"?\n\nIt is currently ${label}.`)) return
    setBusy(true); onError(null)
    try{
      const res = await fetch(`${API_BASE}/books/${book.id}/series/lookup`, {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else if(onChanged) onChanged(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  return (
    <div className="series-cell">
      {label ? <span className="series-name">{label}</span> : <span className="muted">Standalone</span>}
      {!readOnly && (
        <button type="button" onClick={lookup} disabled={busy}
                title="Fetch the series from OpenLibrary, falling back to the published title on Google Books">
          {busy ? '...' : (label ? 'Refresh' : 'Lookup')}
        </button>
      )}
    </div>
  )
}

// How much of a description is shown when the row is collapsed. Long enough to
// tell two books apart, short enough that the table stays a table.
const DESCRIPTION_PREVIEW = 90

function DescriptionCell({book, expanded, onToggle, onChanged, onError}){
  const readOnly = useReadOnly()
  const [busy,setBusy]=useState(false)
  const text = t(book.description)

  const lookup = async ()=>{
    if(text && !confirm(`Re-fetch the description for "${book.title}"?\n\nThe stored one, including any you wrote yourself, will be replaced.`)) return
    setBusy(true); onError(null)
    try{
      const res = await fetch(`${API_BASE}/books/${book.id}/description/lookup`, {method:'POST', headers: authHeaders()})
      if(!res.ok){ onError(await readError(res)) }
      else if(onChanged) onChanged(await res.json())
    }catch(err){ onError(friendlyMessage(err.message)) }
    setBusy(false)
  }

  const preview = text.length > DESCRIPTION_PREVIEW ? text.slice(0, DESCRIPTION_PREVIEW).trimEnd() + '…' : text

  return (
    <div className="description-cell">
      {text
        ? <button type="button" className="description-toggle" onClick={onToggle}
                  aria-expanded={expanded} title={expanded ? 'Hide the description' : 'Show the full description'}>
            <span className="description-caret" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
            <span className="description-preview">{expanded ? 'Hide description' : preview}</span>
          </button>
        : <span className="muted">None</span>}
      {!readOnly && (
        <button type="button" onClick={lookup} disabled={busy}
                title="Fetch the publisher blurb from Google Books, falling back to OpenLibrary">
          {busy ? '...' : (text ? 'Refresh' : 'Lookup')}
        </button>
      )}
    </div>
  )
}

// --- shelves ---
// The drawn bookcase, ported from the iOS app's BookshelfView so both clients
// show the same thing. Geometry is in SVG user units; the viewBox scales it to
// whatever room the page has.
const ART_SLOT_W = 115
const ART_SLOT_H = 100
const ART_INSET = 14
const ART_BOARD = 6
const ART_DIVIDER = 4
// How far the located slot lifts out of the row, as if pulled off the shelf.
const ART_LIFT = 11

function shelfArtGeometry(shelf){
  const columns = Math.max(shelf.columns || 1, 1)
  const rows = Math.max(shelf.rows || 1, 1)
  const innerW = columns * ART_SLOT_W
  const innerH = rows * ART_SLOT_H
  const cell = (ci, ri)=>{
    const x = ART_INSET + ci * ART_SLOT_W
    const y = ART_INSET + ri * ART_SLOT_H
    return {x, y, w: ART_SLOT_W, h: ART_SLOT_H, midX: x + ART_SLOT_W/2, midY: y + ART_SLOT_H/2, maxY: y + ART_SLOT_H}
  }
  return {columns, rows, innerW, innerH, width: innerW + ART_INSET*2, height: innerH + ART_INSET*2, cell}
}

// One spine standing in a slot. Books are a fixed width whatever the slot holds
// and stack from the left upright, the way a part-filled shelf really looks, so
// how full a cell is can be read without counting.
function shelfArtSpine(g, ci, ri, index, capacity, heightFraction){
  const cell = g.cell(ci, ri)
  const usable = Math.max(cell.h - ART_BOARD, 1)
  const height = usable * heightFraction
  const band = Math.max(cell.w - ART_DIVIDER*2 - 4, 1)
  const gap = capacity > 1 ? 2 : 0
  const width = Math.max((band - gap * (capacity - 1)) / capacity, 1)
  const start = cell.midX - band/2
  return {x: start + index * (width + gap), y: cell.maxY - ART_BOARD - height, w: width, h: height}
}

// Stable pseudo-random, so a shelf looks the same every time it is opened.
function shelfArtMix(a, b, salt){
  let x = (Math.imul(a, 73856093) ^ Math.imul(b, 19349663) ^ Math.imul(salt, 83492791)) >>> 0
  x ^= x >>> 16; x = Math.imul(x, 2246822507) >>> 0
  x ^= x >>> 13; x = Math.imul(x, 3266489909) >>> 0
  return (x ^ (x >>> 16)) >>> 0
}

const ART_PALETTE = ['#993333', '#335980', '#407352', '#8c6b2e', '#61427a', '#804d38', '#38475c']

// Neighbouring spines are pushed apart in the palette as well as picked from it,
// so two books side by side never come out the same colour and merge together.
function shelfArtSpineColor(column, row, index){
  const base = shelfArtMix(column, row, 7) % ART_PALETTE.length
  const step = 1 + shelfArtMix(column, row, 23) % (ART_PALETTE.length - 1)
  return ART_PALETTE[(base + step * index) % ART_PALETTE.length]
}

function shelfArtHeightFraction(column, row, index){
  return 0.62 + (shelfArtMix(column, row * 31 + index, 11) % 30) / 100
}

// The backdrop in every slot shares one height, so it reads as a single quiet
// band behind the books rather than as varied books of its own.
const ART_EMPTY_HEIGHT = 0.91

// How many spines still read as books rather than slivers, given how wide the
// shelf is. A 20 column shelf on a phone gets one apiece.
function shelfArtCapacity(columns){
  if(columns <= 8) return 3
  if(columns <= 14) return 2
  return 1
}

function usePrefersReducedMotion(){
  const [reduced,setReduced] = useState(()=>
    typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false)
  useEffect(()=>{
    if(!window.matchMedia) return
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = ()=> setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return ()=> mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}

/// A drawn bookshelf with the locating animation: crosshairs sweep to the slot,
/// the rest of the case dims, and the book lifts and pulses while the view
/// pushes in on it.
function BookshelfGraphic({shelf, slots, highlight, runId}){
  const reduced = usePrefersReducedMotion()
  // 0 nothing yet, 1 crosshairs sweeping, 2 landed on the slot, 3 pulsing.
  const [phase,setPhase] = useState(0)

  const hasTarget = !!(highlight && highlight.column && highlight.row)

  useEffect(()=>{
    if(!hasTarget){ setPhase(0); return }
    if(reduced){ setPhase(2); return }  // land on the answer without the sweep
    setPhase(0)
    const timers = [
      setTimeout(()=> setPhase(1), 250),
      setTimeout(()=> setPhase(2), 900),
      setTimeout(()=> setPhase(3), 1450),
    ]
    return ()=> timers.forEach(clearTimeout)
  }, [runId, hasTarget, reduced, highlight && highlight.column, highlight && highlight.row])

  if(!shelf) return null
  const g = shelfArtGeometry(shelf)
  const sweeping = phase >= 1
  const landed = phase >= 2
  const pulsing = phase >= 3 && !reduced

  const occupied = {}
  ;(slots || []).forEach(s=>{
    const key = `${s.column},${s.row}`
    ;(occupied[key] = occupied[key] || []).push(s)
  })

  const targetCi = hasTarget ? highlight.column - 1 : -1
  const targetRi = hasTarget ? highlight.row - 1 : -1
  const target = hasTarget ? g.cell(targetCi, targetRi) : null
  const capacity = shelfArtCapacity(g.columns)

  // Push in on the located slot. Done as an explicit translate+scale rather
  // than a transform-origin, which browsers place differently inside SVG.
  const zoom = (landed && target && !reduced) ? 1.18 : 1
  const tx = target ? target.midX - zoom * target.midX : 0
  const ty = target ? target.midY - zoom * target.midY : 0
  const stageStyle = {transform: `translate(${tx}px, ${ty}px) scale(${zoom})`,
                      transition: 'transform .75s cubic-bezier(.2,.7,.3,1)'}

  // Crosshairs grow from the top left to the slot. y' = INSET + p(y - INSET),
  // written as a translate so no transform-origin is needed.
  const p = sweeping ? 1 : 0
  const guideV = {transform: `translate(0px, ${ART_INSET * (1 - p)}px) scale(1, ${p})`,
                  transition: 'transform .65s ease-out'}
  const guideH = {transform: `translate(${ART_INSET * (1 - p)}px, 0px) scale(${p}, 1)`,
                  transition: 'transform .65s ease-out'}

  const slotCells = []
  for(let ri = 0; ri < g.rows; ri++){
    for(let ci = 0; ci < g.columns; ci++){
      const cell = g.cell(ci, ri)
      const isTarget = ci === targetCi && ri === targetRi
      const here = occupied[`${ci+1},${ri+1}`] || []
      const drawn = Math.min(here.length, capacity)
      const backdrop = shelfArtSpine(g, ci, ri, 0, 1, ART_EMPTY_HEIGHT)
      const spines = []
      for(let i = 0; i < drawn; i++){
        spines.push({...shelfArtSpine(g, ci, ri, i, capacity, shelfArtHeightFraction(ci, ri, i)),
                     color: shelfArtSpineColor(ci, ri, i), key: i})
      }
      // Everything but the located slot fades back, so the answer is the only
      // thing left bright.
      const dim = hasTarget && landed && !isTarget
      const body = (
        <g className={dim ? 'shelf-art-slot dim' : 'shelf-art-slot'} key={`${ci},${ri}`}>
          <rect x={backdrop.x} y={backdrop.y} width={backdrop.w} height={backdrop.h} rx="3"
                fill="url(#shelfArtEmpty)" stroke="rgba(0,0,0,.12)" strokeWidth="0.5" />
          {isTarget ? (
            // Centred so the lift and the pulse can be plain transforms.
            <g transform={`translate(${cell.midX} ${cell.midY})`}>
              <g style={{transform: landed ? `translateY(${-ART_LIFT}px)` : 'none',
                         transition: 'transform .45s cubic-bezier(.2,1.3,.4,1)'}}>
                <g className={pulsing ? 'shelf-art-pulse' : ''}>
                  {spines.map(s=> (
                    <rect key={s.key} x={s.x - cell.midX} y={s.y - cell.midY} width={s.w} height={s.h} rx="2"
                          fill={s.color} stroke="#facc15" strokeWidth="2" className="shelf-art-spine lit" />
                  ))}
                  {drawn === 0 && (
                    <rect x={backdrop.x - cell.midX} y={backdrop.y - cell.midY}
                          width={backdrop.w} height={backdrop.h} rx="3"
                          fill="rgba(250,204,21,.18)" stroke="#facc15" strokeWidth="2" />
                  )}
                </g>
              </g>
            </g>
          ) : spines.map(s=> (
            <rect key={s.key} x={s.x} y={s.y} width={s.w} height={s.h} rx="2"
                  fill={s.color} stroke="rgba(0,0,0,.25)" strokeWidth="0.5" className="shelf-art-spine" />
          ))}
        </g>
      )
      slotCells.push(body)
    }
  }

  const label = hasTarget
    ? `Bookshelf ${shelf.name}. The book is at column ${highlight.column}, row ${highlight.row}.`
    : `Bookshelf ${shelf.name}`

  return (
    <svg className="shelf-art" viewBox={`0 0 ${g.width} ${g.height}`} role="img" aria-label={label}>
      <defs>
        <linearGradient id="shelfArtBack" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2e211a" />
          <stop offset="100%" stopColor="#241a14" />
        </linearGradient>
        <linearGradient id="shelfArtBoard" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#5c3d29" />
          <stop offset="100%" stopColor="#3d2619" />
        </linearGradient>
        <linearGradient id="shelfArtUpright" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#5c3d29" />
          <stop offset="100%" stopColor="#3d2619" />
        </linearGradient>
        <linearGradient id="shelfArtEmpty" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="rgba(255,255,255,.07)" />
          <stop offset="100%" stopColor="rgba(255,255,255,.03)" />
        </linearGradient>
      </defs>

      <g style={stageStyle}>
        <rect x="1" y="1" width={g.width - 2} height={g.height - 2} rx="10"
              fill="url(#shelfArtBack)" stroke="#3d2619" strokeWidth="10" />

        {/* Uprights first: the sides run the height, the boards sit between them. */}
        {Array.from({length: g.columns - 1}, (_, i)=> (
          <rect key={`d${i}`} x={ART_INSET + (i+1) * ART_SLOT_W - ART_DIVIDER/2} y={ART_INSET}
                width={ART_DIVIDER} height={g.innerH} fill="url(#shelfArtUpright)" />
        ))}
        {Array.from({length: g.rows}, (_, i)=> (
          <rect key={`b${i}`} x={ART_INSET} y={ART_INSET + (i+1) * ART_SLOT_H - ART_BOARD}
                width={g.innerW} height={ART_BOARD} fill="url(#shelfArtBoard)" />
        ))}

        {slotCells}

        {hasTarget && (
          <>
            <rect x={target.midX - 1} y={ART_INSET} width="2" height={g.innerH}
                  fill="rgba(250,204,21,.55)" style={guideV} />
            <rect x={ART_INSET} y={target.midY - 1} width={g.innerW} height="2"
                  fill="rgba(250,204,21,.55)" style={guideH} />
            <g transform={`translate(${target.midX} ${target.midY})`}
               style={{opacity: landed ? 1 : 0, transition: 'opacity .35s ease-out'}}>
              <g className={pulsing ? 'shelf-art-pulse' : ''}>
                <rect x={-target.w * 0.46} y={-target.h * 0.46} width={target.w * 0.92} height={target.h * 0.92}
                      rx="5" fill="none" stroke="#facc15" strokeWidth="2.5" />
              </g>
              {/* The top row has no shelf above it to hang a pointer in, so its
                  arrow points up from below instead of off the top edge.
                  Placement is a transform attribute on an outer group: a CSS
                  animation sets the transform property, which would otherwise
                  replace the attribute and drop the arrow into the slot. */}
              <g transform={`translate(0 ${targetRi > 0 ? -target.h * 0.62 : target.h * 0.62})`}>
                <g className={pulsing ? 'shelf-art-arrow' : ''}>
                  <path d="M -13 -10 L 13 -10 L 0 10 Z" fill="#facc15"
                        transform={targetRi > 0 ? undefined : 'rotate(180)'} />
                </g>
              </g>
            </g>
          </>
        )}
      </g>
    </svg>
  )
}

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

/// Shows where a book sits, with the locating animation. Read-only, so guests
/// get it too.
function ShelfLocator({book, shelves, onClose}){
  const [layout,setLayout] = useState(null)
  const [error,setError] = useState(null)
  const [loading,setLoading] = useState(true)
  // Bumped to replay the animation.
  const [runId,setRunId] = useState(0)

  const placed = !!(book.shelf_id && book.shelf_column && book.shelf_row)
  const location = placed ? {column: book.shelf_column, row: book.shelf_row} : null

  useEffect(()=>{
    let cancelled = false
    if(!placed){ setLoading(false); return }
    ;(async ()=>{
      try{
        const res = await fetch(`${API_BASE}/shelves/${book.shelf_id}/layout`, {headers: authHeaders()})
        if(cancelled) return
        if(!res.ok){ setError(await readError(res)) }
        else setLayout(await res.json())
      }catch(err){ if(!cancelled) setError(friendlyMessage(err.message)) }
      finally{ if(!cancelled) setLoading(false) }
    })()
    return ()=>{ cancelled = true }
  }, [book.id, book.shelf_id])

  // Falling back to the shelf list keeps the drawing possible when only the
  // layout call failed.
  const shelf = (layout && layout.shelf) || (shelves || []).find(s=> s.id === book.shelf_id)

  useEffect(()=>{
    const onKey = (e)=>{ if(e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return ()=> window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal locator" onClick={e=> e.stopPropagation()}>
        <div className="locator-head">
          <div style={{minWidth:0}}>
            <h3 style={{margin:0}}>Find on shelf</h3>
            <div className="locator-title">{book.title}</div>
            {shelf && <div className="locator-shelf">{shelf.name}</div>}
          </div>
          <button type="button" onClick={onClose}>Done</button>
        </div>

        {error && <div className="alert" style={{marginTop:10}}>{error}</div>}

        {!placed ? (
          <div className="locator-empty">
            <div className="locator-empty-icon">?</div>
            <strong>No location yet</strong>
            <span>Nobody has recorded where “{book.title}” lives. Use Place to set a shelf and slot.</span>
          </div>
        ) : loading ? (
          <div className="locator-empty"><span>Loading the shelf...</span></div>
        ) : !shelf ? (
          // The book does have a position; we just could not fetch the shelf to
          // draw it. Saying "no location" here would be a lie.
          <div className="locator-empty">
            <div className="locator-empty-icon">!</div>
            <strong>Couldn't load the shelf</strong>
            <span>“{book.title}” is at column {book.shelf_column}, row {book.shelf_row}, but the shelf could not be fetched.</span>
          </div>
        ) : (
          <>
            <div className="shelf-frame locator-frame">
              <BookshelfGraphic shelf={shelf} slots={layout && layout.slots}
                                highlight={location} runId={runId} />
            </div>
            <div className="locator-coords">
              <div className="locator-coord">
                <span>COLUMN</span>
                <strong>{book.shelf_column}</strong>
                <span>of {shelf.columns}</span>
              </div>
              <div className="locator-coord-divider" />
              <div className="locator-coord">
                <span>ROW</span>
                <strong>{book.shelf_row}</strong>
                <span>of {shelf.rows}</span>
              </div>
              <button type="button" style={{marginLeft:'auto'}} onClick={()=> setRunId(v=> v+1)}>
                Play again
              </button>
            </div>
          </>
        )}
      </div>
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
  const readOnly = useReadOnly()
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
      <div style={{color:'#666'}}>{readOnly ? 'No shelves have been set up yet.' : 'No shelves yet — add one below to get started.'}</div></div>
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
                     onDropBook={readOnly ? undefined : moveBook}
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
              {inSlot.length>0 && !readOnly && (
                <div className="drag-hint">
                  Drag a book onto a slot to move it{shelves.length>1 ? ', or onto a shelf name to move it there' : ''}.
                </div>
              )}
              {inSlot.length===0 && <div className="slot-detail-empty">Nothing here yet.</div>}
              {inSlot.map(b=> (
                <div key={b.id}
                     className={dragging===b.id ? 'slot-book dragging' : 'slot-book'}
                     draggable={!readOnly}
                     onDragStart={e=>{
                       if(readOnly){ e.preventDefault(); return }
                       e.dataTransfer.setData('text/plain', String(b.id))
                       e.dataTransfer.effectAllowed = 'move'
                       setDragging(b.id)
                     }}
                     onDragEnd={()=> setDragging(null)}>
                  {!readOnly && <span className="drag-handle" title="Drag onto a slot to move this book">⠿</span>}
                  <CoverThumb book={b} width={34} />
                  <div style={{minWidth:0,flex:1}}>
                    <div className="slot-book-title">{b.title}</div>
                    {b.author && <div className="slot-book-author">{b.author}</div>}
                    {b.tags && b.tags.length>0 && (
                      <div className="slot-book-tags">{b.tags.slice(0,3).join(' · ')}</div>
                    )}
                  </div>
                  {!readOnly && (
                    <div className="nowrap">
                      <button type="button" onClick={()=> onPlace(b)}>Move</button>
                      <button type="button" onClick={()=> onDelete(b)} style={{marginLeft:6}}>Delete</button>
                    </div>
                  )}
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
function LocationCell({book, shelves, onPlace, onLocate}){
  const readOnly = useReadOnly()
  const shelf = (shelves || []).find(s=> s.id === book.shelf_id)
  const placed = book.shelf_id && book.shelf_column && book.shelf_row
  const where = placed
    ? `${shelf? shelf.name : 'Shelf'} — column ${book.shelf_column}, row ${book.shelf_row}`
    : 'Not placed on a shelf'
  const locate = (placed && onLocate) ? (
    <button type="button" className="locate-btn" onClick={()=> onLocate(book)}
            title={`Show where “${book.title}” is on the shelf`}>
      ◎ Locate
    </button>
  ) : null
  if(readOnly){
    return (
      <div className="location-cell">
        <span className={placed? 'location-btn placed' : 'location-btn'} title={where}>
          {placed
            ? <>
                <span className="location-shelf">{shelf? shelf.name : 'Shelf'}</span>
                <span className="location-coord">{book.shelf_column},{book.shelf_row}</span>
              </>
            : <span className="location-empty muted">Not placed</span>}
        </span>
        {locate}
      </div>
    )
  }
  return (
    <div className="location-cell">
      <button type="button" className={placed? 'location-btn placed' : 'location-btn'}
              onClick={()=> onPlace(book)}
              title={placed? where : 'Set where this book lives'}>
        {placed
          ? <>
              <span className="location-shelf">{shelf? shelf.name : 'Shelf'}</span>
              <span className="location-coord">{book.shelf_column},{book.shelf_row}</span>
            </>
          : <span className="location-empty">Place</span>}
      </button>
      {locate}
    </div>
  )
}

function SourcesCell({book, onChanged, onError}){
  const readOnly = useReadOnly()
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
          : readOnly ? <span className="muted">—</span>
          : <button type="button" onClick={()=>lookup('olid')} disabled={!!busy || !book.isbn}
                    title={book.isbn ? 'Look up the OpenLibrary edition id from the ISBN' : 'Add an ISBN first'}>
              {busy==='olid' ? '...' : 'Lookup'}
            </button>}
      </div>
      <div className="source-row">
        <span className="source-label">GB</span>
        {book.google_id
          ? <a className="source-link" href={`https://books.google.com/books?id=${book.google_id}`} target="_blank" rel="noreferrer" title={book.google_id}>{book.google_id}</a>
          : readOnly ? <span className="muted">—</span>
          : <button type="button" onClick={()=>lookup('google')} disabled={!!busy || (!book.isbn && !book.title)}
                    title="Look up the Google Books volume id">
              {busy==='google' ? '...' : 'Lookup'}
            </button>}
      </div>
    </div>
  )
}

function AuthorCell({author}){
  const [expanded,setExpanded] = useState(false)
  const authors = t(author).split(',').map(t).filter(Boolean)
  if(!authors.length) return <span className="muted">—</span>

  const hidden = Math.max(0, authors.length - 2)
  const visible = expanded ? authors : authors.slice(0, 2)
  return (
    <div className="author-cell">
      <span>{visible.join(', ')}</span>
      {hidden>0 && (
        <button type="button" className="author-more" onClick={()=>setExpanded(!expanded)}
                aria-expanded={expanded}>
          {expanded ? 'Show fewer' : `+${hidden} more`}
        </button>
      )}
    </div>
  )
}

function CheckoutStatus({book, onCheckout, onCheckin, busy}){
  if(!book.checked_out_at){
    return (
      <div className="checkout-status">
        <span className="available">Available</span>
        {onCheckout && <button type="button" className="primary" onClick={()=>onCheckout(book)} disabled={busy}>Checkout</button>}
      </div>
    )
  }
  return (
    <div className="checkout-status checked-out">
      <strong>Checked out</strong>
      <span>{book.borrower_name}</span>
      <span>{formatCheckout(book.checked_out_at)}</span>
      {onCheckin && <button type="button" onClick={()=>onCheckin(book)} disabled={busy}>Check in</button>}
    </div>
  )
}

function CheckoutPanel({onBookPatched}){
  const readOnly = useReadOnly()
  const [books,setBooks] = useState([])
  const [q,setQ] = useState('')
  const [busy,setBusy] = useState(null)
  const [error,setError] = useState(null)

  const load = async (query)=>{
    const term = t(query===undefined ? q : query)
    const params = ['sort=title', 'dir=asc', 'checked_out=true']
    if(term) params.push('q=' + encodeURIComponent(term))
    try{
      const res = await fetch(API_BASE + '/books?' + params.join('&'), {headers: authHeaders()})
      if(!res.ok){ setError(await readError(res)); return }
      setBooks(await res.json())
      setError(null)
    }catch(err){ setError(friendlyMessage(err.message)) }
  }

  useEffect(()=>{ load('') }, [])

  const checkin = async (book)=>{
    setBusy(book.id); setError(null)
    try{
      const updated = await checkinBook(book)
      setBooks(prev=> prev.filter(b=> b.id!==book.id))
      if(onBookPatched) onBookPatched(updated)
    }catch(err){ setError(friendlyMessage(err.message)); await load() }
    finally{ setBusy(null) }
  }

  return (
    <div className="card checkout-panel">
      <h3>Checked out books</h3>
      <form className="search-row" onSubmit={e=>{ e.preventDefault(); load() }}>
        <input placeholder="Search title, author, notes or ISBN" value={q} onChange={e=>setQ(e.target.value)} />
        <button type="submit">Search</button>
        <button type="button" onClick={()=>{ setQ(''); load('') }}>Clear</button>
      </form>
      {error && <div className="alert checkout-error">{error}</div>}
      <div className="checkout-list">
        {books.map(book=> (
          <div className="checkout-book" key={book.id}>
            <CoverThumb book={book} width={42} />
            <div className="checkout-book-details">
              <strong>{book.title}</strong>
              {book.author && <span>{book.author}</span>}
            </div>
            <CheckoutStatus book={book} onCheckin={readOnly ? undefined : checkin} busy={busy===book.id} />
          </div>
        ))}
        {!books.length && <div className="muted checkout-empty">No checked-out books match.</div>}
      </div>
    </div>
  )
}

function SortHeader({label, field, sort, onSort, className}){
  if(!onSort) return <th className={className}>{label}</th>
  const active = sort && sort.field === field
  return (
    <th className={['sortable', className].filter(Boolean).join(' ')}>
      <button type="button" className={active? 'sort-btn active':'sort-btn'} onClick={()=>onSort(field)}
              aria-label={`Sort by ${label}`}>
        {label}<span className="sort-arrow">{active ? (sort.dir==='asc' ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>
  )
}

function BooksTable({books, onDelete, onSaved, onBookPatched, emptyText, sort, onSort, shelves, onPlace, onLocate}){
  const readOnly = useReadOnly()
  const [editingId, setEditingId] = useState(null)
  const [editVals, setEditVals] = useState({title:'', author:'', isbn:'', olid:'', googleId:'', notes:'', tags:'', format:'', series:'', seriesIndex:'', description:'', added:'', addedOriginal:''})
  // Which row the message belongs to, not just the message: an error printed
  // above a long table is off screen for the row that caused it, which reads as
  // the button having done nothing at all.
  const [rowError, setRowError] = useState(null)
  const [circulationBusy, setCirculationBusy] = useState(null)
  // Descriptions are paragraphs, so they are collapsed by default and opened a
  // row at a time. Ids rather than a single id: reading two blurbs side by side
  // is the whole point of having them in a list.
  const [openDescriptions, setOpenDescriptions] = useState({})
  const toggleDescription = (id)=> setOpenDescriptions(prev=> ({...prev, [id]: !prev[id]}))
  const showRowError = (bookId, message)=> setRowError(message ? {bookId, message} : null)

  const changeCirculation = async (book, action)=>{
    setCirculationBusy(book.id)
    setRowError(null)
    try{
      const updated = action==='checkout' ? await checkoutBook(book) : await checkinBook(book)
      if(updated && onBookPatched) onBookPatched(updated)
    }catch(err){ showRowError(book.id, friendlyMessage(err.message)) }
    finally{ setCirculationBusy(null) }
  }

  const startEdit = (b)=>{
    setRowError(null)
    setEditingId(b.id)
    const added = toDateInput(b.created_at)
    setEditVals({title: b.title||'', author: b.author||'', isbn: b.isbn||'', olid: b.olid||'', googleId: b.google_id||'', notes: b.notes||'', tags: (b.tags||[]).join(', '), format: b.format||'', series: b.series||'', seriesIndex: (b.series_index===null||b.series_index===undefined) ? '' : String(b.series_index), description: b.description||'', added, addedOriginal: added})
  }
  const cancelEdit = ()=>{ setEditingId(null); setRowError(null); setEditVals({title:'', author:'', isbn:'', olid:'', googleId:'', notes:'', tags:'', format:'', series:'', seriesIndex:'', description:'', added:'', addedOriginal:''}) }

  const saveEdit = async (book)=>{
    setRowError(null)
    const vals = {title: t(editVals.title), author: t(editVals.author), isbn: t(editVals.isbn), olid: t(editVals.olid),
                  google_id: t(editVals.googleId), notes: t(editVals.notes),
                  tags: editVals.tags.split(',').map(t).filter(Boolean),
                  format: t(editVals.format),
                  series: t(editVals.series),
                  series_index: t(editVals.seriesIndex) === '' ? null : Number(editVals.seriesIndex),
                  description: t(editVals.description)}
    if(!vals.title){ showRowError(book.id, 'Title is required'); return }
    if(vals.series_index !== null && (isNaN(vals.series_index) || vals.series_index < 0)){
      showRowError(book.id, 'Series number must be a number, like 2 or 2.5'); return
    }
    if(vals.isbn && !validateISBN(vals.isbn)){ showRowError(book.id, 'ISBN must be 10 or 13 digits'); return }
    if(vals.olid){
      const olid = normalizeOlid(vals.olid)
      if(!olid){ showRowError(book.id, olidProblem(vals.olid)); return }
      vals.olid = olid
    }
    if(vals.google_id){
      const googleId = normalizeGoogleId(vals.google_id)
      if(!googleId){ showRowError(book.id, 'Google ID must look like otCEEQAAQBAJ'); return }
      vals.google_id = googleId
    }
    // Only send the added date when it was actually changed, so an untouched row
    // keeps the time-of-day part of its original timestamp.
    if(editVals.added !== editVals.addedOriginal){
      if(!editVals.added){ showRowError(book.id, 'Date added cannot be cleared'); return }
      vals.created_at = editVals.added
    }
    try{
      const res = await fetch(API_BASE + '/books/' + book.id, {method: 'PUT', headers: authHeaders(true), body: JSON.stringify(vals)})
      if(!res.ok){ showRowError(book.id, await readError(res)); return }
      const updated = await res.json()
      cancelEdit()
      if(onSaved) onSaved(updated, book)
    }catch(err){ showRowError(book.id, friendlyMessage(err.message)) }
  }

  if(!books || books.length===0){
    return <div style={{padding:12,color:'#666'}}>{emptyText || 'No books yet.'}</div>
  }

  return (
    <div style={{overflowX:'auto',maxWidth:'100%'}}>
      <table className="books">
        <thead><tr>
          <th>Cover</th>
          <SortHeader label="Title" field="title" sort={sort} onSort={onSort} />
          <SortHeader label="Author" field="author" sort={sort} onSort={onSort} className="col-author" />
          <th className="col-isbn">ISBN</th>
          <th>Sources</th>
          <SortHeader label="Location" field="location" sort={sort} onSort={onSort} />
          <SortHeader label="Series" field="series" sort={sort} onSort={onSort} />
          <th>Tags</th>
          <th>Format</th>
          <th className="col-description">Description</th>
          <th className="col-notes">Notes</th>
          <SortHeader label="Added" field="added" sort={sort} onSort={onSort} />
          <th>Status</th>
          {!readOnly && <th></th>}
        </tr></thead>
        <tbody>
          {books.map(b=> (
            <Fragment key={b.id}>
            <tr>
              <td><CoverCell book={b} onChanged={onBookPatched} onError={m=> showRowError(b.id, m)} /></td>
              {editingId===b.id ? (
                <>
                  <td><input value={editVals.title} onChange={e=>setEditVals({...editVals, title: e.target.value})} /></td>
                  <td className="col-author"><input value={editVals.author} onChange={e=>setEditVals({...editVals, author: e.target.value})} /></td>
                  <td className="col-isbn"><input value={editVals.isbn} onChange={e=>setEditVals({...editVals, isbn: e.target.value})} /></td>
                  <td>
                    <input value={editVals.olid} placeholder="OL12345M" onChange={e=>setEditVals({...editVals, olid: e.target.value})} />
                    <input style={{marginTop:4}} value={editVals.googleId} placeholder="otCEEQAAQBAJ" onChange={e=>setEditVals({...editVals, googleId: e.target.value})} />
                  </td>
                  <td className="nowrap"><LocationCell book={b} shelves={shelves} onPlace={onPlace} onLocate={onLocate} /></td>
                  <td>
                    <input value={editVals.series} placeholder="Series name"
                           onChange={e=>setEditVals({...editVals, series: e.target.value})} />
                    <input style={{marginTop:4}} value={editVals.seriesIndex} placeholder="#" inputMode="decimal"
                           onChange={e=>setEditVals({...editVals, seriesIndex: e.target.value})} />
                  </td>
                  <td><input value={editVals.tags} placeholder="comma, separated, tags" onChange={e=>setEditVals({...editVals, tags: e.target.value})} /></td>
                  <td>
                    <input list="known-formats" value={editVals.format} placeholder="Paperback"
                           onChange={e=>setEditVals({...editVals, format: e.target.value})} />
                  </td>
                  <td className="col-description">
                    <textarea rows={3} value={editVals.description} placeholder="Description"
                              onChange={e=>setEditVals({...editVals, description: e.target.value})} />
                  </td>
                  <td className="col-notes"><input value={editVals.notes} onChange={e=>setEditVals({...editVals, notes: e.target.value})} /></td>
                  <td><input type="date" value={editVals.added} onChange={e=>setEditVals({...editVals, added: e.target.value})} /></td>
                  <td><CheckoutStatus book={b}
                                      onCheckout={book=>changeCirculation(book, 'checkout')}
                                      onCheckin={readOnly ? undefined : book=>changeCirculation(book, 'checkin')}
                                      busy={circulationBusy===b.id} /></td>
                  <td className="nowrap">
                    <button onClick={()=>saveEdit(b)}>Save</button>
                    <button onClick={cancelEdit} style={{marginLeft:6}}>Cancel</button>
                  </td>
                </>
              ) : (
                <>
                  <td className="col-title">{b.title}</td>
                  <td className="col-author"><AuthorCell author={b.author} /></td>
                  <td className="col-isbn nowrap">{b.isbn}</td>
                  <td><SourcesCell book={b} onChanged={onBookPatched} onError={m=> showRowError(b.id, m)} /></td>
                  <td className="nowrap"><LocationCell book={b} shelves={shelves} onPlace={onPlace} onLocate={onLocate} /></td>
                  <td><SeriesCell book={b} onChanged={onBookPatched} onError={m=> showRowError(b.id, m)} /></td>
                  <td><TagsCell book={b} onChanged={onBookPatched} onError={m=> showRowError(b.id, m)} /></td>
                  <td className="nowrap"><FormatCell book={b} onChanged={onBookPatched} onError={m=> showRowError(b.id, m)} /></td>
                  <td className="col-description">
                    <DescriptionCell book={b} expanded={!!openDescriptions[b.id]}
                                     onToggle={()=>toggleDescription(b.id)}
                                     onChanged={onBookPatched} onError={m=> showRowError(b.id, m)} />
                  </td>
                  <td className="col-notes">{b.notes}</td>
                  <td className="nowrap">{formatAdded(b.created_at)}</td>
                  <td><CheckoutStatus book={b}
                                      onCheckout={book=>changeCirculation(book, 'checkout')}
                                      onCheckin={readOnly ? undefined : book=>changeCirculation(book, 'checkin')}
                                      busy={circulationBusy===b.id} /></td>
                  {!readOnly && (
                    <td className="nowrap">
                      <button onClick={()=>startEdit(b)}>Edit</button>
                      <button onClick={()=>onDelete(b)} style={{marginLeft:6}}>Delete</button>
                    </td>
                  )}
                </>
              )}
            </tr>
            {openDescriptions[b.id] && editingId!==b.id && t(b.description) && (
              <tr className="description-row">
                <td colSpan={readOnly ? 13 : 14}>
                  <div className="description-full">{b.description}</div>
                </td>
              </tr>
            )}
            {rowError && rowError.bookId===b.id && (
              <tr className="row-error">
                <td colSpan={readOnly ? 13 : 14}><div className="alert">{rowError.message}</div></td>
              </tr>
            )}
            </Fragment>
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
  // null until /me answers. Assuming either way would flash the wrong UI.
  const [me,setMe]=useState(null)
  const [meError,setMeError]=useState(null)
  const [undo,setUndo]=useState(null)
  const [undoTimer,setUndoTimer]=useState(null)
  const [sort,setSort]=useState({field:'added', dir:'desc'})
  const [allTags,setAllTags]=useState([])
  const [selectedTags,setSelectedTags]=useState([])
  const [excludedTags,setExcludedTags]=useState([])
  // '' is any format, '__none__' is the books with none recorded.
  const [formatFilter,setFormatFilter]=useState('')
  // '' is every shelf, '__unplaced__' is books with no recorded location.
  const [shelfFilter,setShelfFilter]=useState('')
  const [formatsInUse,setFormatsInUse]=useState([])
  // '' is every series, '__none__' is the standalones.
  const [seriesFilter,setSeriesFilter]=useState('')
  const [seriesInUse,setSeriesInUse]=useState([])
  const [tagMatch,setTagMatch]=useState('all')
  const [refreshing,setRefreshing]=useState(null)
  const [shelves,setShelves]=useState([])
  const [placing,setPlacing]=useState(null)
  // The book whose shelf position is being shown. Read-only, so guests get it.
  const [locating,setLocating]=useState(null)
  // Bumped whenever a location changes, so the bookshelf browser refetches.
  const [locationVersion,setLocationVersion]=useState(0)

  const readOnly = !!(me && me.read_only)

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
    const f = {tags: selectedTags, excludes: excludedTags, match: tagMatch,
               shelf: shelfFilter, ...(filterOverride || {})}
    const fmt = f.format===undefined ? formatFilter : f.format
    const ser = f.series===undefined ? seriesFilter : f.series
    const params = [`sort=${s.field}`, `dir=${s.dir}`]
    if(term) params.push('q=' + encodeURIComponent(term))
    if(f.tags && f.tags.length){
      params.push('tags=' + encodeURIComponent(f.tags.join(',')))
      params.push('match=' + f.match)
    }
    if(f.excludes && f.excludes.length){
      params.push('exclude_tags=' + encodeURIComponent(f.excludes.join(',')))
    }
    if(f.shelf==='__unplaced__') params.push('placed=false')
    else if(f.shelf) params.push('shelf_id=' + encodeURIComponent(f.shelf))
    // "No format" is its own question rather than a value, so it travels as
    // has_format=false instead of as a magic binding name.
    if(fmt==='__none__') params.push('has_format=false')
    else if(fmt) params.push('format=' + encodeURIComponent(fmt))
    // Same shape as the format filter: "no series" is its own question, not a
    // series called None.
    if(ser==='__none__') params.push('has_series=false')
    else if(ser) params.push('series=' + encodeURIComponent(ser))
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

  const toggleTag = (name, mode)=>{
    if(mode==='exclude'){
      const excludes = excludedTags.includes(name) ? excludedTags.filter(x=> x!==name) : [...excludedTags, name]
      const includes = selectedTags.filter(x=> x!==name)
      setExcludedTags(excludes)
      setSelectedTags(includes)
      fetchBooks(undefined, undefined, {tags: includes, excludes})
      return
    }
    const includes = selectedTags.includes(name) ? selectedTags.filter(x=> x!==name) : [...selectedTags, name]
    const excludes = excludedTags.filter(x=> x!==name)
    setSelectedTags(includes)
    setExcludedTags(excludes)
    fetchBooks(undefined, undefined, {tags: includes, excludes})
  }

  const changeTagMatch = (mode)=>{
    setTagMatch(mode)
    if(selectedTags.length) fetchBooks(undefined, undefined, {match: mode})
  }

  const changeFormatFilter = (value)=>{
    setFormatFilter(value)
    fetchBooks(undefined, undefined, {format: value})
  }

  const changeShelfFilter = (value)=>{
    setShelfFilter(value)
    fetchBooks(undefined, undefined, {shelf: value})
  }

  const fetchFormats = async ()=>{
    try{
      const res = await fetch(API_BASE + '/formats', {headers: authHeaders()})
      if(res.ok){ const d = await res.json(); setFormatsInUse(d.in_use || []) }
    }catch(e){ console.error('fetch formats failed', e) }
  }

  const fetchSeries = async ()=>{
    try{
      const res = await fetch(API_BASE + '/series', {headers: authHeaders()})
      if(res.ok) setSeriesInUse(await res.json())
    }catch(e){ console.error('fetch series failed', e) }
  }

  const changeSeriesFilter = (value)=>{
    setSeriesFilter(value)
    fetchBooks(undefined, undefined, {series: value})
  }

  const clearTagFilter = ()=>{
    setSelectedTags([])
    setExcludedTags([])
    fetchBooks(undefined, undefined, {tags: [], excludes: []})
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

  // The sweeps over the whole listing share one shape: confirm, walk the books
  // one at a time so the catalogues are not hammered, and patch each row as its
  // answer arrives so progress is visible rather than arriving all at once.
  // A book the lookup has nothing for answers 404, which is not an error worth
  // stopping a sweep of two hundred books for.
  const refreshAllField = async ({endpoint, label, confirmText, after})=>{
    const targets = books.filter(b=> b.isbn || b.olid || b.title)
    if(!targets.length) return
    if(!confirm(confirmText(targets.length))) return
    let found = 0
    for(let i=0;i<targets.length;i++){
      setRefreshing(`${label} ${i+1}/${targets.length}...`)
      try{
        const res = await fetch(`${API_BASE}/books/${targets[i].id}/${endpoint}/lookup`, {method:'POST', headers: authHeaders()})
        if(res.ok){
          found++
          const updated = await res.json()
          setBooks(prev=> prev.map(x=> x.id===updated.id ? {...x, ...updated} : x))
        }
      }catch(e){ console.error(`${endpoint} refresh failed`, e) }
    }
    setRefreshing(null)
    if(after) after()
    alert(`Updated ${found} of ${targets.length} book${targets.length===1?'':'s'}. The rest had nothing on record.`)
  }

  const refreshAllSeries = ()=> refreshAllField({
    endpoint: 'series', label: 'Series',
    confirmText: n=> `Look up the series for ${n} book${n===1?'':'s'}?\n\nAny series recorded by hand will be replaced where a catalogue has one.`,
    after: fetchSeries,
  })

  const refreshAllDescriptions = ()=> refreshAllField({
    endpoint: 'description', label: 'Descriptions',
    confirmText: n=> `Look up descriptions for ${n} book${n===1?'':'s'}?\n\nDescriptions already stored, including any you wrote yourself, will be replaced where one is found.`,
  })

  const loadMe = async ()=>{
    setMeError(null)
    try{
      const res = await fetch(API_BASE + '/me', {headers: authHeaders()})
      if(res.ok){ setMe(await res.json()); return }
      if(res.status===401){ localStorage.removeItem('token'); setLoggedIn(false); return }
      if(res.status===404){
        // A backend older than guest access has no /me and no guests either, so
        // an authenticated session there can only be a full account. Treating
        // this as read-only would lock everyone out of their own library.
        setMe({username: null, role: 'admin', read_only: false})
        return
      }
      setMeError('Could not reach the server. Your session is still signed in.')
    }catch(err){
      setMeError('Could not reach the server. Your session is still signed in.')
    }
  }

  useEffect(()=>{
    if(!loggedIn){ setMe(null); setMeError(null); return }
    loadMe()
  }, [loggedIn])

  // Guests have no Add tab, so send them somewhere that exists.
  useEffect(()=>{
    if(readOnly && tab==='add'){ setTab('manage'); fetchBooks(); fetchTags(); fetchShelves(); fetchFormats(); fetchSeries() }
  }, [readOnly])

  useEffect(()=>{ if(loggedIn){ fetchBooks(); fetchTags(); fetchShelves(); fetchFormats(); fetchSeries() } }, [loggedIn])

  // If an administrator deletes the shelf currently being filtered, return to
  // all shelves instead of leaving the Manage tab stuck on an impossible id.
  useEffect(()=>{
    if(shelfFilter && shelfFilter!=='__unplaced__'
       && !shelves.some(s=> String(s.id)===String(shelfFilter))){
      setShelfFilter('')
      fetchBooks(undefined, undefined, {shelf: ''})
    }
  }, [shelves, shelfFilter])

  // A series can vanish from the library entirely — the last book in it gets
  // deleted, or a refresh renames it — which would otherwise leave the filter
  // stuck on a name that matches nothing.
  useEffect(()=>{
    if(seriesFilter && seriesFilter!=='__none__' && seriesInUse.length
       && !seriesInUse.some(s=> s.name===seriesFilter)){
      setSeriesFilter('')
      fetchBooks(undefined, undefined, {series: ''})
    }
  }, [seriesInUse, seriesFilter])

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
    // A lookup can introduce a binding nothing else had, which should show up
    // in the filter rather than waiting for a reload.
    fetchFormats()
  }

  const logout = ()=>{ localStorage.removeItem('token'); setLoggedIn(false); setMe(null); setBooks([]); setRecent([]); setAllTags([]); setSelectedTags([]); setExcludedTags([]); setFormatFilter(''); setShelfFilter(''); setFormatsInUse([]) }

  return (
    <ReadOnlyContext.Provider value={readOnly}>
    <div className={loggedIn ? 'container wide' : 'container'}>
      <datalist id="known-formats">
        {KNOWN_FORMATS.map(f=> <option key={f} value={f} />)}
      </datalist>
      <div className="header">
        <h1>Book Library</h1>
        {readOnly && <span className="guest-badge" title="Guests can browse and use checkout, but cannot edit the catalogue">Guest</span>}
        {loggedIn && <button onClick={logout}>{readOnly ? 'Leave' : 'Logout'}</button>}
      </div>

      {undo && (
        <div className="snackbar">
          <div>{undo.type==='delete' ? 'Book deleted' : undo.type==='update' ? 'Change saved' : 'Book added'}</div>
          <button onClick={performUndo}>Undo</button>
        </div>
      )}

      {!loggedIn ? <Login onLogin={()=>setLoggedIn(true)} /> : !me ? (
        // Until /me answers we do not know whether this is a guest, and drawing
        // the editor first would offer buttons that answer 403.
        <div className="card">
          {meError ? (
            <>
              <div className="alert">{meError}</div>
              <button type="button" onClick={loadMe}>Try again</button>
              <button type="button" onClick={logout} style={{marginLeft:6}}>Sign out</button>
            </>
          ) : <div style={{color:'#666'}}>Loading...</div>}
        </div>
      ) : (
        <>
          {placing && (
            <ShelfPicker book={placing} shelves={shelves}
                         onClose={()=> setPlacing(null)}
                         onSaved={(updated)=>{ onBookPatched(updated); fetchShelves(); setLocationVersion(v=> v+1) }} />
          )}
          {locating && (
            <ShelfLocator book={locating} shelves={shelves} onClose={()=> setLocating(null)} />
          )}
          <div className="tabs">
            {!readOnly && <button className={tab==='add'? 'tab active':'tab'} onClick={()=>setTab('add')}>Add</button>}
            <button className={tab==='manage'? 'tab active':'tab'} onClick={()=>{ setTab('manage'); fetchBooks(); fetchTags(); fetchShelves(); fetchFormats(); fetchSeries() }}>{readOnly ? 'Browse' : 'Manage'}</button>
            <button className={tab==='checkout'? 'tab active':'tab'} onClick={()=>setTab('checkout')}>Checkout</button>
            <button className={tab==='shelves'? 'tab active':'tab'} onClick={()=>{ setTab('shelves'); fetchShelves() }}>Bookshelf</button>
          </div>

          {tab==='shelves' ? (
            <>
              <BookshelfBrowser shelves={shelves} refreshToken={locationVersion}
                                onPlace={setPlacing} onDelete={handleDelete}
                                onMoved={(updated)=>{ onBookPatched(updated); fetchShelves() }} />
              {!readOnly && <ShelvesPanel shelves={shelves} onChanged={async ()=>{ await fetchShelves(); await fetchBooks(); setLocationVersion(v=> v+1) }} />}
            </>
          ) : tab==='checkout' ? (
            <CheckoutPanel onBookPatched={onBookPatched} />
          ) : (tab==='add' && !readOnly) ? (
            <>
              <AddForm onAdded={onAdded} />
              <div className="card" style={{minWidth:0}}>
                <h3>Recently added this session</h3>
                <BooksTable books={recent} onDelete={handleDelete} onSaved={onSaved} onBookPatched={onBookPatched} shelves={shelves} onPlace={setPlacing} onLocate={setLocating} emptyText="Nothing added yet — books you add will appear here so you can edit them." />
              </div>
            </>
          ) : (
            <div className="card" style={{minWidth:0}}>
              <h3>{readOnly ? 'Browse library' : 'Manage library'}</h3>
              <form className="search-row" onSubmit={e=>{ e.preventDefault(); fetchBooks() }}>
                <input placeholder="Search title, author, notes or ISBN" value={q} onChange={e=>setQ(e.target.value)} />
                <button type="submit">Search</button>
                <button type="button" onClick={()=>{ setQ(''); fetchBooks('') }}>Clear</button>
                <select value={formatFilter} onChange={e=>changeFormatFilter(e.target.value)}
                        title="Show only one binding">
                  <option value="">Any format</option>
                  {formatsInUse.map(f=> <option key={f} value={f}>{f}</option>)}
                  <option value="__none__">No format</option>
                </select>
                <select value={shelfFilter} onChange={e=>changeShelfFilter(e.target.value)}
                        title="Show books on one shelf">
                  <option value="">Any shelf</option>
                  {shelves.map(s=> <option key={s.id} value={String(s.id)}>{s.name}</option>)}
                  <option value="__unplaced__">Not placed</option>
                </select>
                <select value={seriesFilter} onChange={e=>changeSeriesFilter(e.target.value)}
                        title="Show one series">
                  <option value="">Any series</option>
                  {seriesInUse.map(s=> <option key={s.name} value={s.name}>{s.name} ({s.count})</option>)}
                  <option value="__none__">Standalone</option>
                </select>
              </form>
              {!readOnly && books.length>0 && (
                <div className="bulk-actions">
                  <span className="muted">Fill in the whole list from the catalogues:</span>
                  <button type="button" onClick={refreshAllSeries} disabled={!!refreshing}
                          title="Look up the series and volume number for every book listed below">
                    Refresh all series
                  </button>
                  <button type="button" onClick={refreshAllDescriptions} disabled={!!refreshing}
                          title="Look up the description for every book listed below">
                    Refresh all descriptions
                  </button>
                  {refreshing && <span className="bulk-progress">{refreshing}</span>}
                </div>
              )}
              <TagFilter tags={allTags} selected={selectedTags} excluded={excludedTags} match={tagMatch}
                         onToggle={toggleTag} onMatchChange={changeTagMatch} onClear={clearTagFilter}
                         onRefreshAll={(books.length && !readOnly) ? refreshAllTags : null} refreshing={refreshing} />
              <div style={{margin:'8px 0',color:'#666',fontSize:13}}>{books.length} book{books.length===1?'':'s'}</div>
              <BooksTable books={books} onDelete={handleDelete} onSaved={onSaved} onBookPatched={onBookPatched} sort={sort} onSort={toggleSort} shelves={shelves} onPlace={setPlacing} onLocate={setLocating} emptyText="No books match." />
            </div>
          )}
        </>
      )}
    </div>
    </ReadOnlyContext.Provider>
  )
}
