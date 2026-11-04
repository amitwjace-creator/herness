"""Application routes shared by the web UI and local API clients."""
import copy
import json
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .library import Library, Project
from .state import ProjectStore, display_text


class Application:
    def __init__(self,state):
        self.state=state
        self.home=ProjectStore(state.root)
        self.projects=json.loads(self.home.read('projects.json','{}'))
        self.projects.setdefault('default',{'id':'default','title':state.root.name,'path':str(state.root)})
        self.active='default'
        self.bind()

    def bind(self):
        from .workbench import Workbench
        from .connections import Connections
        from .documents import Documents
        from .automation import Automation
        from .models import ModelSettings
        from .privacy import Privacy
        from .vectors import VectorKnowledge
        self.library=Library(self.state.root)
        self.project=Project(self.state.root)
        self.automation=Automation(self.state.root)
        self.privacy=Privacy(self.state.root)
        documents=Documents(self.state.root)
        self.vectors=VectorKnowledge(self.state.root,documents=documents,privacy=self.privacy)
        self.modules=[Workbench(self.state.root),Connections(self.state.root),documents,self.automation,ModelSettings(self.state.root),self.privacy,self.vectors]
        self.state.application=self
        self.state.run_options=json.loads(self.state.store.read('run-options.json','{"mode":"act"}'))
        self.state.project=self.project.read()
        from .runtime import RuntimeLedger
        RuntimeLedger(self.state.root).recover_abandoned()

    def tick(self):
        with self.state.lock:
            for module in self.modules:
                if hasattr(module,'tick'):
                    module.tick(self.state)

    def close(self):
        for module in self.modules:
            if hasattr(module, 'close'):
                module.close()

    def handle(self, method, url, data):
        parsed=urlsplit(url);path=parsed.path;q={k:v[-1] for k,v in parse_qs(parsed.query).items()}
        state=self.state
        if path=='/api/features' and method=='GET':
            from .catalog import catalog
            return catalog()
        if path=='/api/library':
            if method=='POST':
                with state.lock:
                    if state.busy: raise ValueError('Finish or stop the active task before changing conversations')
                    return self.library.update(data)
            sessions=self.library.list(q.get('q',''),q.get('trash')=='true')
            return {'sessions':sessions,'folders':sorted({m.get('folder','') for m in self.library.metadata.values()}-{''})}
        if path=='/api/export' and method=='GET':
            return self.library.export(q.get('session',state.session_name),q.get('format','markdown'))
        if path=='/api/bookmark' and method=='POST':
            with state.lock:
                return self.library.bookmark(state.session_name,data.get('index'),data.get('enabled'))
        if path in {'/api/branch','/api/retry'} and method=='POST':
            with state.lock:
                if state.busy: raise ValueError('Finish or stop the active turn first')
                retry=path.endswith('retry')
                index=next((i for i in range(len(state.session.messages)-1,0,-1) if state.session.messages[i].get('role')=='user'),None) if retry else data.get('index')
                original=state.session
                name,session=self.library.branch(original,index,data.get('prompt'),retry)
                state.session_name,state.session=name,session
                state.messages=state.saved_messages()
                state.streaming=''
                if retry:
                    # Preserve exact multimodal content without decoding/re-uploading files.
                    state.start(display_text(original.messages[index]['content']),prepared_content=copy.deepcopy(original.messages[index]['content']),prepared_summaries=copy.deepcopy(original.attachments.get(str(index),[])))
                elif data.get('prompt') is not None:
                    state.start(data['prompt'])
                return {'session':name,'busy':state.busy}
        if path=='/api/project':
            if method=='POST':
                with state.lock:
                    state.project=self.project.update(data)
            return self.project.read()
        if path=='/api/projects':
            if method=='POST':
                with state.lock:
                    if state.busy: raise ValueError('Finish the active task before switching projects')
                    action=data.get('action')
                    if action=='add':
                        root=data.get('path')
                        if not isinstance(root,str): raise ValueError('Enter an existing project folder')
                        if 'title' in data and (not isinstance(data['title'], str) or len(data['title']) > 200):
                            raise ValueError('Project title must contain at most 200 characters')
                        target=Path(root).expanduser().resolve()
                        if not target.is_dir(): raise ValueError('Project folder does not exist')
                        if len(self.projects)>=100: raise ValueError('At most 100 projects are supported')
                        identity=next((k for k,v in self.projects.items() if v['path']==str(target)),secrets.token_hex(6))
                        self.projects[identity]={'id':identity,'title':data.get('title') or target.name,'path':str(target)}
                        self.home.write('projects.json',json.dumps(self.projects,indent=2))
                    elif action=='switch':
                        identity=data.get('id')
                        if not isinstance(identity, str) or identity not in self.projects: raise ValueError('Project not found')
                        target=Path(self.projects[identity]['path'])
                        replacement=type(state)(target,state.client,state.demo)
                        self.close()
                        token,lock=state.token,state.lock
                        state.__dict__.update(replacement.__dict__)
                        state.token,state.lock=token,lock
                        self.active=identity
                        self.bind()
                    else: raise ValueError('Choose add or switch')
            return {'projects':list(self.projects.values()),'active':self.active}
        if path=='/api/run/options':
            if method=='POST':
                with state.lock:
                    if state.busy: raise ValueError('Finish the active task before changing execution settings')
                    options=dict(state.run_options)
                    if 'mode' in data:
                        if not isinstance(data['mode'], str) or data['mode'] not in {'act','plan'}: raise ValueError('Choose Act or Plan mode')
                        options['mode']=data['mode']
                    limits={'max_steps':(1,100),'max_context_tokens':(100,2000000),'max_output_tokens':(16,64000),'max_cost_usd':(0,10000),'input_usd_per_million':(0,1000),'output_usd_per_million':(0,1000)}
                    for key,(minimum,maximum) in limits.items():
                        if key in data:
                            value=data[key]
                            if value in ('',None): options.pop(key,None);continue
                            if isinstance(value,bool) or not isinstance(value,(int,float)) or not minimum<=value<=maximum: raise ValueError(f'Invalid {key}')
                            options[key]=int(value) if key in {'max_steps','max_context_tokens','max_output_tokens'} else value
                    if options.get('max_cost_usd') is not None and not any(options.get(k) for k in ('input_usd_per_million', 'output_usd_per_million')):
                        raise ValueError('Cost budgets require input or output token prices for your model')
                    state.run_options=options
                    state.store.write('run-options.json',json.dumps(options,indent=2))
            return state.run_options
        if path=='/api/context' and method=='GET':
            from .runtime import context_snapshot
            return {**context_snapshot(state.root,state.session.messages,self.project.read()),'memory':state.store.memory(),'project':self.project.read()}
        if path in {'/api/runs','/api/diagnostics'} and method=='GET':
            from .runtime import RuntimeLedger
            ledger=RuntimeLedger(state.root)
            return ledger.diagnostics() if path.endswith('diagnostics') else (ledger.get_run(q['id']) if q.get('id') else {'runs':ledger.list_runs()})
        if path=='/api/files' and method=='GET':
            result=self.modules[0].handle('GET','/api/workbench/files',{},state)
            return result
        if path=='/api/files/read' and method=='POST':
            from .tools import ToolRunner
            result=ToolRunner(state.root,lambda _:False).execute('read_file',json.dumps({'path':data.get('path')}))
            if 'error' in result: raise ValueError(result['error'])
            return result
        with state.lock:
            modules, vectors, automation = tuple(self.modules), self.vectors, self.automation
        source_mutation = method == 'POST' and path in {
            '/api/documents/upload', '/api/documents/library', '/api/privacy/memory',
            '/api/privacy/memory/import', '/api/privacy/retention/apply', '/api/privacy/backup/restore'}
        for module in modules:
            if module is automation or path == '/api/workbench/undo' or source_mutation or (method == 'POST' and path == '/api/models/profiles'):
                with state.lock:
                    if self.vectors is not vectors:
                        raise ValueError('The active project changed. Retry this action in the current project.')
                    result=module.handle(method,path,{**q,**data},state)
                    if result is not None and source_mutation:
                        vectors.invalidate_sources()
            else:
                result=module.handle(method,path,{**q,**data},state)
            if result is not None:
                return result
        return None
