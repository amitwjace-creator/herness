"""Conversation organization, reversible branches, and project configuration."""
import copy
import json
import re
import secrets
import time
from pathlib import Path

from .core import Session
from .state import ProjectStore, display_text


class Library:
    def __init__(self, root):
        self.root = Path(root)
        self.store = ProjectStore(root)
        self.metadata = json.loads(self.store.read('library.json', '{}'))

    def save(self):
        self.store.write('library.json', json.dumps(self.metadata, indent=2))

    def session(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[\w-]{1,64}', name, flags=re.ASCII):
            raise ValueError('Invalid conversation name')
        path = self.store.directory / 'sessions' / (name + '.json')
        if not path.is_file() or path.is_symlink():
            raise ValueError('Conversation not found')
        return Session(self.root, name)

    def list(self, query='', trash=False):
        items = []
        directory = self.store.directory / 'sessions'
        if directory.is_symlink():
            raise ValueError('Session directory must not be a symlink')
        for path in directory.glob('*.json'):
            try:
                if path.is_symlink() or path.stat().st_size > 64_000_000:
                    continue
                session = self.session(path.stem)
                meta = self.metadata.get(path.stem, {})
                if bool(meta.get('trashed')) != trash:
                    continue
                first = next((display_text(m.get('content')) for m in session.messages if m.get('role') == 'user'), '')
                if not first:
                    continue
                title = meta.get('title') or ' '.join(first.split())[:100]
                searchable = '\n'.join([title, meta.get('folder', ''), ' '.join(meta.get('tags', []))] + [display_text(m.get('content')) for m in session.messages if m.get('role') in {'user','assistant'}])
                if query.casefold() not in searchable.casefold():
                    continue
                items.append({'name': path.stem, 'title': title, 'updated': path.stat().st_mtime,
                              'pinned': bool(meta.get('pinned')), 'folder': meta.get('folder',''),
                              'tags': meta.get('tags', []), 'trashed': bool(meta.get('trashed'))})
            except (ValueError, OSError, KeyError, TypeError):
                continue
        return sorted(items,key=lambda x:(not x['pinned'],-x['updated']))[:500]

    def update(self, data):
        names, action = data.get('names'), data.get('action')
        if not isinstance(names,list) or not 1 <= len(names) <= 500:
            raise ValueError('Select one or more conversations')
        for name in names:
            self.session(name)
        if action not in {'update','trash','restore'}:
            raise ValueError('Unknown conversation action')
        values = data.get('values', {})
        if not isinstance(values,dict):
            raise ValueError('Invalid conversation metadata')
        clean = {}
        for key in ('title','folder'):
            if key in values:
                if not isinstance(values[key],str) or len(values[key]) > 200:
                    raise ValueError('Title and folder must contain at most 200 characters')
                clean[key] = values[key].strip()
        if 'tags' in values:
            tags=values['tags']
            if not isinstance(tags,list) or len(tags)>20 or any(not isinstance(t,str) or len(t)>50 for t in tags):
                raise ValueError('Use at most 20 short tags')
            clean['tags']=list(dict.fromkeys(t.strip() for t in tags if t.strip()))
        if 'pinned' in values:
            if not isinstance(values['pinned'],bool):
                raise ValueError('Pinned must be boolean')
            clean['pinned']=values['pinned']
        for name in names:
            meta=self.metadata.setdefault(name,{})
            if action=='update': meta.update(clean)
            else: meta['trashed']=action=='trash'
        self.save()
        return {'ok':True}

    def bookmark(self, name, index, enabled):
        session=self.session(name)
        if not isinstance(index,int) or not 0<index<len(session.messages) or not isinstance(enabled,bool):
            raise ValueError('Invalid bookmark')
        meta=self.metadata.setdefault(name,{})
        bookmarks=set(meta.get('bookmarks',[]))
        if enabled: bookmarks.add(index)
        else: bookmarks.discard(index)
        meta['bookmarks']=sorted(bookmarks)
        self.save()
        return {'ok':True}

    def branch(self, original, index, prompt=None, retry=False):
        if not isinstance(index,int) or not 0<index<len(original.messages):
            raise ValueError('Choose a conversation message')
        message=original.messages[index]
        if message.get('role') not in {'user', 'assistant'}:
            raise ValueError('Choose a user or assistant message to branch')
        if prompt is not None and (not isinstance(prompt,str) or not prompt.strip() or len(prompt)>32000):
            raise ValueError('Enter a nonempty edited message')
        if (prompt is not None or retry) and message.get('role')!='user':
            raise ValueError('Only user messages can be edited or retried')
        end=index if prompt is not None or retry else index+1
        # A branch can never leave tool-call messages missing their responses.
        while end>1 and original.messages[end-1].get('tool_calls'):
            end-=1
        name='branch-'+secrets.token_hex(8)
        result=Session(self.root,name)
        result.messages=copy.deepcopy(original.messages[:end])
        result.attachments={k:copy.deepcopy(v) for k,v in original.attachments.items() if int(k)<end}
        result.save()
        self.metadata[name]={'parent':original.path.stem,'parent_index':index}
        self.save()
        return name,result

    def export(self, name, format='markdown'):
        session=self.session(name)
        if format=='json':
            return {'filename':name+'.json','mime':'application/json','content':json.dumps({'version':1,'messages':session.messages,'attachments':session.attachments},indent=2)}
        if format!='markdown': raise ValueError('Choose markdown or json')
        lines=['# '+(self.metadata.get(name,{}).get('title') or name),'']
        for i,m in enumerate(session.messages):
            if m.get('role') not in {'user','assistant'}: continue
            lines+=['## '+m['role'].capitalize(),'',display_text(m.get('content')),'']
            if session.attachments.get(str(i)): lines+=['Attachments: '+', '.join(x['name'] for x in session.attachments[str(i)]),'']
        return {'filename':name+'.md','mime':'text/markdown','content':'\n'.join(lines)}


class Project:
    def __init__(self, root):
        self.root=Path(root)
        self.store=ProjectStore(root)

    def read(self):
        defaults={'title':self.root.name,'instructions':'','goals':'','ignored_paths':[], 'pinned_files':[]}
        saved=json.loads(self.store.read('project.json','{}'))
        return {**defaults,**saved}

    def update(self,data):
        current=self.read()
        for key in ('title','instructions','goals'):
            if key in data:
                if not isinstance(data[key],str) or len(data[key])>16000: raise ValueError('Project text exceeds 16000 characters')
                current[key]=data[key]
        for key in ('ignored_paths','pinned_files'):
            if key in data:
                if not isinstance(data[key],list) or len(data[key])>100 or any(not isinstance(v,str) or len(v)>500 for v in data[key]): raise ValueError('Invalid project paths')
                current[key]=data[key]
        self.store.write('project.json',json.dumps(current,indent=2))
        return current
