import React, { useState, useEffect } from 'react'

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
  const [notes,setNotes]=useState('')
  const [error,setError]=useState(null)
  const [loading,setLoading]=useState(false)
  const [searching,setSearching]=useState(false)
  const [searchResults,setSearchResults]=useState([])
  const [expandedSet,setExpandedSet]=useState({})
  const [allLanguages,setAllLanguages]=useState(false)
  const toggleExpanded = (idx)=> setExpandedSet(prev=> ({...prev, [idx]: !prev[idx]}))

  const submit=async e=>{
    e.preventDefault()
    setError(null)
    const vals = {title: t(title), author: t(author), isbn: t(isbn), notes: t(notes)}
    const missing = []
    if(!vals.title) missing.push('Title')
    if(!vals.author) missing.push('Author')
    if(!vals.isbn) missing.push('ISBN')
    if(missing.length){ setError(`${missing.join(', ')} ${missing.length>1?'are':'is'} required to add a book manually`); return }
    if(!validateISBN(vals.isbn)){ setError('ISBN must be 10 or 13 digits'); return }
    try{
      const res = await fetch(API_BASE + '/books',{method:'POST', headers: authHeaders(true), body: JSON.stringify(vals)})
      if(!res.ok){ setError(await readError(res)); return }
      const created = await res.json()
      setTitle(''); setAuthor(''); setIsbn(''); setNotes('')
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
      if(!j.title && (!j.authors || !j.authors.length)) setError('No data found')
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

  const handleAddFromSearch = async (doc, isbnVal, olidVal) =>{
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

    const payload = { title: titleVal, author: authorsVal, isbn: t(isbnVal || (details && details.isbns && details.isbns[0])), notes: olidVal ? `OLID:${olidVal}` : '' }
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
      <form onSubmit={submit}>
        <h3>Add book</h3>
        {error && <div className="alert">{error}</div>}
        <label>Title<input value={title} onChange={e=>setTitle(e.target.value)} required/></label>
        <label>Author<input value={author} onChange={e=>setAuthor(e.target.value)} required/></label>
        <div style={{margin:'8px 0',display:'flex',gap:10,alignItems:'center',flexWrap:'wrap'}}>
          <button type="button" className="primary" onClick={()=>searchMeta()} disabled={searching}>{searching? 'Searching...':'Search editions'}</button>
          <label className="inline-check">
            <input type="checkbox" checked={allLanguages} onChange={e=>{ setAllLanguages(e.target.checked); if(searchResults.length) searchMeta(e.target.checked) }} />
            Include translations
          </label>
        </div>
        <label>ISBN
          <div style={{display:'flex',gap:6}}>
            <input style={{flex:1,minWidth:0}} value={isbn} onChange={e=>setIsbn(e.target.value)} required/>
            <button type="button" onClick={lookup} disabled={loading}>{loading? 'Looking...':'Lookup ISBN'}</button>
          </div>
        </label>
        <label>Notes<input value={notes} onChange={e=>setNotes(e.target.value)}/></label>
        <div style={{marginTop:8}}>
          <button type="submit" className="danger">Manually add</button>
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
                      {((expandedSet[idx]) ? doc.editions : doc.editions.slice(0,6)).map((ed,ii)=> (
                        <button key={ed.olid || ii} type="button" className="edition-card" onClick={()=>handleAddFromSearch(doc, (ed.isbns && ed.isbns[0]) || null, ed.olid)}>
                          {ed.cover
                            ? <img src={ed.cover} alt="cover" className="edition-cover"/>
                            : <div className="edition-cover edition-cover-empty">No cover</div>}
                          <div className="edition-title">{ed.title}</div>
                          <div className="edition-meta">{ed.publish_date || ''}{ed.publishers && ed.publishers.length? ' — '+ed.publishers[0]: ''}</div>
                          {ed.number_of_pages? <div className="edition-meta">{ed.number_of_pages} pages</div> : null}
                        </button>
                      ))}
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

function BooksTable({books, onDelete, onSaved, emptyText}){
  const [editingId, setEditingId] = useState(null)
  const [editVals, setEditVals] = useState({title:'', author:'', isbn:'', notes:''})
  const [rowError, setRowError] = useState(null)

  const startEdit = (b)=>{
    setRowError(null)
    setEditingId(b.id)
    setEditVals({title: b.title||'', author: b.author||'', isbn: b.isbn||'', notes: b.notes||''})
  }
  const cancelEdit = ()=>{ setEditingId(null); setRowError(null); setEditVals({title:'', author:'', isbn:'', notes:''}) }

  const saveEdit = async (book)=>{
    setRowError(null)
    const vals = {title: t(editVals.title), author: t(editVals.author), isbn: t(editVals.isbn), notes: t(editVals.notes)}
    if(!vals.title){ setRowError('Title is required'); return }
    if(vals.isbn && !validateISBN(vals.isbn)){ setRowError('ISBN must be 10 or 13 digits'); return }
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
        <thead><tr><th>Title</th><th>Author</th><th>ISBN</th><th>Notes</th><th></th></tr></thead>
        <tbody>
          {books.map(b=> (
            <tr key={b.id}>
              {editingId===b.id ? (
                <>
                  <td><input value={editVals.title} onChange={e=>setEditVals({...editVals, title: e.target.value})} /></td>
                  <td><input value={editVals.author} onChange={e=>setEditVals({...editVals, author: e.target.value})} /></td>
                  <td><input value={editVals.isbn} onChange={e=>setEditVals({...editVals, isbn: e.target.value})} /></td>
                  <td><input value={editVals.notes} onChange={e=>setEditVals({...editVals, notes: e.target.value})} /></td>
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
                  <td>{b.notes}</td>
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

  const sortNewestFirst = (list)=> Array.isArray(list) ? [...list].sort((a,b)=> (b.id||0)-(a.id||0)) : []

  const fetchBooks = async (query)=>{
    const term = t(query===undefined ? q : query)
    const url = API_BASE + '/books' + (term? '?q='+encodeURIComponent(term): '')
    const res = await fetch(url, {headers: authHeaders()})
    if(res.status===401){ setLoggedIn(false); return }
    setBooks(sortNewestFirst(await res.json()))
  }

  useEffect(()=>{ if(loggedIn) fetchBooks() }, [loggedIn])

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
        const res = await fetch(API_BASE + '/books', {method:'POST', headers: authHeaders(true), body: JSON.stringify({title: undo.book.title, author: undo.book.author, isbn: undo.book.isbn, notes: undo.book.notes})})
        if(res.ok){
          const restored = await res.json()
          if(undo.wasRecent) setRecent(prev=> sortNewestFirst([restored, ...prev]))
        }
      } else if(undo.type==='update'){
        const res = await fetch(API_BASE + '/books/' + undo.book.id, {method:'PUT', headers: authHeaders(true), body: JSON.stringify({title: undo.book.title, author: undo.book.author, isbn: undo.book.isbn, notes: undo.book.notes})})
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
  }

  const onAdded = (created)=>{
    setUndoWithTimeout({type:'add', book: created})
    setRecent(prev=> [created, ...prev.filter(x=> x.id!==created.id)])
    setBooks(prev=> [created, ...prev.filter(x=> x.id!==created.id)])
  }

  const onSaved = (updated, previous)=>{
    setUndoWithTimeout({type:'update', book: previous})
    setBooks(prev=> prev.map(x=> x.id===updated.id ? updated : x))
    setRecent(prev=> prev.map(x=> x.id===updated.id ? updated : x))
  }

  const logout = ()=>{ localStorage.removeItem('token'); setLoggedIn(false); setBooks([]); setRecent([]) }

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
            <button className={tab==='manage'? 'tab active':'tab'} onClick={()=>{ setTab('manage'); fetchBooks() }}>Manage</button>
          </div>

          {tab==='add' ? (
            <>
              <AddForm onAdded={onAdded} />
              <div className="card" style={{minWidth:0}}>
                <h3>Recently added this session</h3>
                <BooksTable books={recent} onDelete={handleDelete} onSaved={onSaved} emptyText="Nothing added yet — books you add will appear here so you can edit them." />
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
              <div style={{margin:'8px 0',color:'#666',fontSize:13}}>{books.length} book{books.length===1?'':'s'}</div>
              <BooksTable books={books} onDelete={handleDelete} onSaved={onSaved} emptyText="No books match." />
            </div>
          )}
        </>
      )}
    </div>
  )
}
