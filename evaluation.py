#!/usr/bin/env python3
"""Small deterministic root-scout evaluation set and transparent scorer."""
import argparse
import json
import pathlib
import statistics
import sys
import hashlib

import oracle_tools

ROOT = pathlib.Path(__file__).resolve().parent
DATASET = ROOT / "evaluation_samples.jsonl"
RANGE_ANCHOR = oracle_tools.CONTRACTS["range_sum.v1"]["statement_anchor"]
MAX_ANCHOR = oracle_tools.CONTRACTS["max.v1"]["statement_anchor"]
RANGE_MIN_ANCHOR = oracle_tools.CONTRACTS["range_min.v1"]["statement_anchor"]


def tool_contract(kind, statement, domain):
    spec=oracle_tools.CONTRACTS[kind]
    query_kind=kind in ("range_sum.v1","range_min.v1")
    domain_quote=("1<=n,q<=200000; -1000000000<=a[i]<=1000000000" if query_kind
                  else "1<=n<=200000; -1000000000<=a[i]<=1000000000")
    return {"kind":kind,"version":1,"caller_attested":True,"reviewed_by":"root-scout evaluation annotation",
            "statement_sha256":oracle_tools.hashlib.sha256(statement.encode()).hexdigest(),
            "statement_quote":spec["statement_anchor"],"semantics":spec["semantics"],"input_format":spec["input_format"],
            "domain":{"n_min":1,"n_max":200000,"value_min":-1000000000,"value_max":1000000000,
                      **({"q_min":1,"q_max":200000} if query_kind else {})},"domain_quote":domain_quote}


def sources(kind, lang, bug):
    if kind=="range_min.v1":
        if lang=="python3":
            if bug=="variant":
                return ("n,q,*data=map(int,__import__('sys').stdin.buffer.read().split())\na=data[:n]\n"
                        "print(*(min(a[data[n+2*i]-1:data[n+2*i+1]]) for i in range(q)),sep='\\n')")
            init="0" if bug in ("init","init_and_endpoint","triple") else "a[l-1]"
            start="l" if bug in ("endpoint","init_and_endpoint","triple") else "l-1"
            compare=">" if bug=="triple" else "<"
            return ("n,q,*data=map(int,__import__('sys').stdin.buffer.read().split())\na=data[:n]\nfor i in range(q):\n"
                    " l,r=data[n+2*i:n+2*i+2]\n"+f" best={init}\n"+f" for x in a[{start}:r]:\n"
                    f"  if x{compare}best: best=x\n print(best)")
        if bug=="variant":
            return ("#include <iostream>\n#include <vector>\n#include <algorithm>\n#include <climits>\nusing namespace std;\nint main(){\n"
                    "int n,q;cin>>n>>q;vector<long long>a(n);for(auto&x:a)cin>>x;\n"
                    "while(q--){int l,r;cin>>l>>r;long long best=LLONG_MAX;\n"
                    "for(int i=l-1;i<r;++i)best=min(best,a[i]);\ncout<<best<<'\\n';}}")
        init="0" if bug in ("init","init_and_endpoint","triple") else "a[l-1]"
        start="l" if bug in ("endpoint","init_and_endpoint","triple") else "l-1"
        compare=">" if bug=="triple" else "<"
        return ("#include <iostream>\n#include <vector>\nusing namespace std;\nint main(){\n"
                "int n,q; cin>>n>>q;\nvector<long long> a(n);\nfor(auto& x:a) cin>>x;\nwhile(q--){\n"
                "int l,r;cin>>l>>r;\n"+f"long long best={init};\n"+f"for(int i={start};i<r;++i){{\n"
                f"if(a[i]{compare}best) best=a[i];}}\n"+"cout<<best<<'\\n';}}")
    if kind=="range_sum.v1":
        if lang=="python3":
            if bug=="variant": return "n,q,*data=map(int,__import__('sys').stdin.buffer.read().split())\na=data[:n]\nprint(*(sum(a[data[n+2*i]-1:data[n+2*i+1]]) for i in range(q)),sep='\\n')"
            expression="sum(a[l:r])" if bug in ("endpoint","both") else "sum(a[l-1:r])"
            if bug in ("overflow","both"):
                # Python integers do not overflow; this label is never applied to Python.
                raise ValueError("range overflow fixtures require C++")
            return f"n,q,*data=map(int,__import__('sys').stdin.buffer.read().split())\na=data[:n]\nfor i in range(q):\n l,r=data[n+2*i:n+2*i+2]\n print({expression})"
        if bug=="variant":
            return ("#include <iostream>\n#include <vector>\nusing namespace std;\nint main(){\nint n,q;cin>>n>>q;vector<long long>a(n);for(auto&x:a)cin>>x;\n"
                    "while(q--){int l,r;cin>>l>>r;long long total=0;for(int i=l-1;i<r;++i)total+=a[i];cout<<total<<'\\n';}}")
        if bug=="triple":
            return ("#include <iostream>\n#include <vector>\nusing namespace std;\nint main(){\nint n,q; cin>>n>>q;\n"
                    "vector<int> p(n+1);\nfor(int i=0;i<n;++i){int x;cin>>x;p[i+1]=p[i]+x;}\n"
                    "while(q-- > 1){\nint l,r;cin>>l>>r;\ncout<<p[r]-p[l]<<'\\n';}}")
        if bug=="quad":
            return ("#include <iostream>\n#include <vector>\nusing namespace std;\nint main(){\nint n,q; cin>>n>>q;\n"
                    "vector<int> p(n+1);\nfor(int i=0;i<n;++i){int x;cin>>x;p[i+1]=p[i]+x;}\n"
                    "while(q-- > 1){\nint l,r;cin>>l>>r;\nlong long ans=p[r]-p[l];\ncout<<ans;\n}\ncout<<'\\n';}")
        vector_type="int" if bug in ("overflow","both") else "long long"
        query="p[r]-p[l]" if bug in ("endpoint","both") else "p[r]-p[l-1]"
        return ("#include <iostream>\n#include <vector>\nusing namespace std;\nint main(){\n"
                "int n,q; cin>>n>>q;\n"+f"vector<{vector_type}> p(n+1);\n"
                "for(int i=0;i<n;++i){int x;cin>>x;p[i+1]=p[i]+x;}\n"
                "while(q--){int l,r;cin>>l>>r;cout<<"+query+"<<'\\n';}}")
    if lang=="python3":
        if bug=="variant": return "n,*a=map(int,__import__('sys').stdin.buffer.read().split())\na=a[:n]\nprint(max(a))"
        init="0" if bug in ("init","init_and_skip") else "a[0]"
        loop="a[:-1]" if bug in ("skip_last","init_and_skip") else "a"
        return f"n,*a=map(int,__import__('sys').stdin.buffer.read().split())\na=a[:n]\nbest={init}\nfor x in {loop}:\n if x>best: best=x\nprint(best)"
    if bug=="variant":
        return "#include <iostream>\n#include <vector>\n#include <algorithm>\nusing namespace std;\nint main(){\nint n;cin>>n;vector<int>a(n);for(auto&x:a)cin>>x;\ncout<<*max_element(a.begin(),a.end())<<'\\n';}"
    if bug=="masked_v2":
        return ("#include <iostream>\n#include <vector>\nusing namespace std;\nint main(){\nint n; cin>>n;\n"
                "vector<long long> a(n);\nfor(auto& x:a) cin>>x;\nlong long best=0;\n"
                "for(int i=0;i<n-1;++i) if(a[i]>best) best=a[i];\ncout<<best<<'\\n';}")
    init="0" if bug in ("init","init_and_skip") else "a[0]"
    loop="n-1" if bug in ("skip_last","init_and_skip") else "n"
    return ("#include <iostream>\n#include <vector>\nusing namespace std;\nint main(){\n"
            "int n;cin>>n;vector<int>a(n);for(auto&x:a)cin>>x;\n"+f"int best={init};\n"
            f"for(int i=0;i<{loop};++i)if(a[i]>best)best=a[i];\ncout<<best<<'\\n';}}")


def build_dataset():
    rows=[]
    def add(case_id,kind,lang,bug,category,root_specs=(),statement_extra=None):
        if category=="missing_spec":
            phrase=statement_extra or "Read a sequence and report the required result."
            anchor=None
        elif kind=="range_sum.v1":
            domain="1<=n,q<=200000; -1000000000<=a[i]<=1000000000"
            anchor=RANGE_ANCHOR; phrase=anchor+" Constraints: "+domain
        elif kind=="max.v1":
            domain="1<=n<=200000; -1000000000<=a[i]<=1000000000"
            anchor=MAX_ANCHOR; phrase=anchor+" Constraints: "+domain
        elif kind=="range_min.v1":
            domain="1<=n,q<=200000; -1000000000<=a[i]<=1000000000"
            anchor=RANGE_MIN_ANCHOR; phrase=anchor+" Constraints: "+domain
        else:
            phrase=statement_extra or "Read integers and produce the required result."
            anchor=None
        request={"evaluation_case_id":case_id,"statement":phrase,"source":sources(kind,lang,bug),"language":lang,"known_failing_tests":[]}
        if anchor:
            request["oracle_tool_contract"]=tool_contract(kind,phrase,{})
        contract,tests,error=(None,[],"not_configured")
        if anchor:
            contract,error=oracle_tools.validate_contract(phrase,request["oracle_tool_contract"])
            if contract:
                contract["contract_sha256"]=hashlib.sha256(json.dumps(contract,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
                tests=oracle_tools.generate(contract)
        frozen=[]
        for root_id,line,input_index,explanation,independence in root_specs:
            if not tests: raise ValueError("annotated root requires oracle cases")
            trigger=tests[input_index]["input"]
            source_lines=request["source"].splitlines()
            frozen.append({"frozen_id":root_id,"explanation":explanation,"source_line":line,"position":source_lines[line-1].strip(),"legal_trigger_input":trigger,"independence":independence})
        rows.append({"case_id":case_id,"category":category,"request":request,"expected_roots":frozen,
                     "annotation_status":"manual_development_fixture","oracle_expected_status":"bug" if frozen else ("clean" if category=="clean" else "unresolved"),
                     "oracle_plan_status":error})
    # Clean controls: both languages and both declared tool kinds.
    for kind in ("range_sum.v1","max.v1"):
        for lang in ("python3","cpp17"):
            add(f"clean-{kind.split('.')[0]}-{lang}",kind,lang,"correct","clean")
    # Single-root controls with distinct contracts, source positions and triggers.
    for lang in ("python3","cpp17"):
        add(f"range-endpoint-{lang}","range_sum.v1",lang,"endpoint","single",
            [("RANGE_ENDPOINT",5 if lang=="python3" else 8,2,"The 1-based closed interval includes element l; the slice/prefix expression starts after it.","independent endpoint defect")])
        add(f"max-init-{lang}","max.v1",lang,"init","single",
            [("MAX_NEGATIVE_INIT",3 if lang=="python3" else 6,1,"best starts at zero although every legal value may be negative.","independent initialization defect")])
    for lang in ("cpp17","cpp20"):
        add(f"range-overflow-{lang}","range_sum.v1",lang,"overflow","single",
            [("RANGE_INT_WIDTH",6,3,"The prefix accumulator uses signed int although the declared values and n permit sums above INT_MAX.","independent numeric-width defect")])
    # Two independent defects and a masking-prone case: endpoint and int overflow.
    for index,lang in enumerate(("cpp17","cpp20")):
        add(f"range-double-{lang}","range_sum.v1",lang,"both","double" if index==0 else "masked",
            [("RANGE_ENDPOINT",8,2,"The closed interval query subtracts p[l], omitting a[l].","independent of prefix width"),
             ("RANGE_INT_WIDTH",6,3,"The prefix accumulator overflows signed int for large legal sums.","independent of endpoint formula")])
    for lang in ("python3","cpp17"):
        add(f"max-double-{lang}","max.v1",lang,"init_and_skip","masked",
            [("MAX_NEGATIVE_INIT",3 if lang=="python3" else 6,1,"The initial best value 0 is invalid when every element is negative.","can mask omitted-final-element defect"),
             ("MAX_SKIP_LAST",4 if lang=="python3" else 7,3,"The loop excludes the final array element, which may be the unique maximum.","independent after initialization is corrected")])
    # Correct boundary implementations with different legal ranges / input shapes.
    for kind,lang in (("range_sum.v1","python3"),("range_sum.v1","cpp17"),("max.v1","python3"),("max.v1","cpp17")):
        add(f"boundary-clean-{kind.split('.')[0]}-{lang}",kind,lang,"variant","clean")
    # Missing/ambiguous specification controls deliberately have no oracle contract.
    missing_statements=("Read a sequence and report the best result. Input bounds and the meaning of best are not specified.",
                        "Given several values, choose the requested aggregate; the task does not define whether to output a sum, count, or maximum.",
                        "A list is provided. The objective and whether negative entries are allowed are omitted.",
                        "Process the numbers and print the answer. The input format, bounds, and output meaning are unspecified.")
    for i,(lang,statement) in enumerate(zip(("python3","cpp17","python3","cpp17"),missing_statements),1):
        add(f"missing-spec-{i}-{lang}","max.v1",lang,"correct","missing_spec",statement_extra=statement)
    # A third declared tool kind plus multi-defect combinations that were never
    # used while tuning the controller prompts.
    for lang in ("python3","cpp17"):
        add(f"clean-range_min-{lang}","range_min.v1",lang,"variant","clean")
    add("range-min-init-python3","range_min.v1","python3","init","single",
        [("RANGE_MIN_INIT",5,0,"best starts at zero, but the minimum of a legal all-positive interval is above zero.","independent initialization defect")])
    add("range-min-init-cpp17","range_min.v1","cpp17","init","single",
        [("RANGE_MIN_INIT",10,0,"best starts at zero, but the minimum of a legal all-positive interval is above zero.","independent initialization defect")])
    add("range-min-double-python3","range_min.v1","python3","init_and_endpoint","double",
        [("RANGE_MIN_INIT",5,0,"best starts at zero instead of the first interval element.","independent of interval bounds"),
         ("RANGE_MIN_ENDPOINT",6,1,"The slice starts at l instead of l-1, omitting element l from the interval.","independent of initialization")])
    add("range-min-triple-cpp17","range_min.v1","cpp17","triple","triple",
        [("RANGE_MIN_INIT",10,0,"best starts at zero instead of the first interval element.","independent of bounds and comparison"),
         ("RANGE_MIN_ENDPOINT",11,1,"The loop starts at l instead of l-1, omitting element l.","independent of initialization"),
         ("RANGE_MIN_COMPARE",12,1,"The comparison keeps the largest element instead of the smallest.","independent of initialization and bounds")])
    add("range-triple-cpp17","range_sum.v1","cpp17","triple","triple",
        [("RANGE_ENDPOINT",10,1,"The closed interval query subtracts p[l], omitting a[l].","independent of prefix width"),
         ("RANGE_INT_WIDTH",6,3,"The prefix accumulator overflows signed int for large legal sums.","independent of endpoint formula"),
         ("RANGE_QUERY_LOOP",8,1,"The query loop drops the final query, so one expected answer line is never printed.","independent output-count defect")])
    add("max-masked-cpp20","max.v1","cpp20","masked_v2","masked",
        [("MAX_NEGATIVE_INIT",8,1,"best starts at zero although every legal value may be negative.","can mask omitted-final-element defect"),
         ("MAX_SKIP_LAST",9,3,"The loop excludes the final array element, which may be the unique maximum.","independent after initialization is corrected")])
    # Fourth root exercises the documented three-root intervention cap; the
    # Python triple exercises the same k=3 variant graph without C++.
    add("range-min-triple-python3","range_min.v1","python3","triple","triple",
        [("RANGE_MIN_INIT",5,0,"best starts at zero instead of the first interval element.","independent of bounds and comparison"),
         ("RANGE_MIN_ENDPOINT",6,1,"The slice starts at l instead of l-1, omitting element l.","independent of initialization"),
         ("RANGE_MIN_COMPARE",7,1,"The comparison keeps the largest element instead of the smallest.","independent of initialization and bounds")])
    add("range-quad-cpp17","range_sum.v1","cpp17","quad","quad",
        [("RANGE_ENDPOINT",10,1,"The closed interval query subtracts p[l], omitting a[l].","independent of prefix width"),
         ("RANGE_INT_WIDTH",6,3,"The prefix accumulator overflows signed int for large legal sums.","independent of endpoint formula"),
         ("RANGE_QUERY_LOOP",8,1,"The query loop drops the final query, so one expected answer line is never printed.","independent output-count defect"),
         ("RANGE_OUTPUT_JOIN",11,1,"Answers are printed without a separator, so distinct results are concatenated.","independent output-format defect")])
    if len(rows)!=32: raise AssertionError(f"expected 32 fixtures, got {len(rows)}")
    return rows


def write_dataset(path=DATASET):
    rows=validate_dataset(build_dataset())
    pathlib.Path(path).write_text("".join(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n" for row in rows),encoding="utf-8")
    return len(rows)


def validate_dataset(rows):
    if not 20<=len(rows)<=40: raise ValueError("evaluation set must contain 20 to 40 frozen samples")
    seen=set(); categories=set(); languages=set()
    for row in rows:
        req=row.get("request",{}); case_id=row.get("case_id")
        key=(req.get("language"),req.get("statement"),req.get("source"))
        if not case_id or req.get("evaluation_case_id")!=case_id or key in seen:
            raise ValueError("evaluation case IDs and (language, statement, source) must be unique and consistent")
        seen.add(key); categories.add(row.get("category")); languages.add(req.get("language"))
        if any(k in req for k in ("expected_roots","frozen_roots","annotation_status")):
            raise ValueError("gold annotations must stay outside the model request")
        oracle_contract,tests,error=(None,[],"not_configured")
        if req.get("oracle_tool_contract"):
            oracle_contract,error=oracle_tools.validate_contract(req["statement"],req["oracle_tool_contract"])
            if oracle_contract:
                oracle_contract["contract_sha256"]=hashlib.sha256(json.dumps(oracle_contract,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
                tests=oracle_tools.generate(oracle_contract)+oracle_tools.generate(oracle_contract,"residual")
        if row.get("category")=="missing_spec" and (oracle_contract or row.get("expected_roots")):
            raise ValueError("missing-spec fixture must not activate an Oracle or contain gold roots")
        if row.get("category")!="missing_spec" and (error or not oracle_contract):
            raise ValueError(f"evaluation fixture {case_id} has an invalid explicit Oracle contract: {error}")
        root_ids=set()
        for root in row.get("expected_roots",[]):
            line=root.get("source_line")
            if root.get("frozen_id") in root_ids or not isinstance(line,int) or not 1<=line<=len(req.get("source","").splitlines()):
                raise ValueError(f"evaluation fixture {case_id} has an invalid frozen root identity/location")
            root_ids.add(root["frozen_id"])
            if root.get("position")!=req["source"].splitlines()[line-1].strip() or not any(t["input"]==root.get("legal_trigger_input") for t in tests):
                raise ValueError(f"evaluation fixture {case_id} root trigger or source location is not bound to its contract")
        if bool(row.get("expected_roots"))!=(row.get("oracle_expected_status")=="bug"):
            raise ValueError(f"evaluation fixture {case_id} has inconsistent oracle_expected_status")
    if not {"clean","single","double","masked","missing_spec"}.issubset(categories) or not {"python3","cpp17","cpp20"}.issubset(languages):
        raise ValueError("evaluation set is missing a required category or language")
    return rows


def read_jsonl(path):
    return validate_dataset([json.loads(line) for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if line.strip()])

def read_raw_jsonl(path):
    return [json.loads(line) for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def load_states(paths):
    states={}
    for raw in paths:
        path=pathlib.Path(raw)
        candidates=list(path.rglob("case_state.json")) if path.is_dir() else [path]
        for candidate in candidates:
            try: state=json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError): continue
            case_id=state.get("request_snapshot",{}).get("evaluation_case_id")
            if case_id: states[case_id]=state
    return states


def run_dataset(dataset, out_dir, provider):
    import root_scout
    out=pathlib.Path(out_dir); out.mkdir(parents=True,exist_ok=True)
    for case in dataset:
        req=case["request"]; folder=out/case["case_id"]; folder.mkdir(parents=True,exist_ok=True)
        try:
            state=root_scout.diagnose(req,provider,str(folder/"case_state.inprogress.json"))
        except KeyboardInterrupt:
            progress=folder/"case_state.inprogress.json"
            state=json.loads(progress.read_text(encoding="utf-8")) if progress.exists() else {"id":case["case_id"],"request_snapshot":req,"roots":[],"evidence":[],"budget":{"tokens_total":0,"wall_seconds":0,"usage_complete":False,"calls":[]},"coverage":{"residual_status":"interrupted","execution_failures":[],"untested":[]}}
            state["stop_reason"]="interrupted"; state.setdefault("coverage",{}).setdefault("untested",[]).append("Evaluation runner interrupted; saved the latest checkpoint.")
            (folder/"case_state.json").write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
            (folder/"report.md").write_text(root_scout.markdown(state),encoding="utf-8")
            break
        except Exception as exc:
            progress=folder/"case_state.inprogress.json"
            if progress.exists():
                state=json.loads(progress.read_text(encoding="utf-8")); state["stop_reason"]="failed"
                state.setdefault("analysis_errors",[]).append({"phase":"evaluation_runner","type":type(exc).__name__,"message":str(exc)})
                state.setdefault("coverage",{}).setdefault("untested",[]).append("Evaluation runner failed after a saved checkpoint.")
            else:
                state={"id":case["case_id"],"request_snapshot":req,"stop_reason":"failed","roots":[],"evidence":[],
                       "budget":{"tokens_total":0,"wall_seconds":0,"usage_complete":False,"calls":[]},
                       "coverage":{"residual_status":"failed","execution_failures":[],"untested":[str(exc)]},
                       "analysis_errors":[{"phase":"evaluation_runner","type":type(exc).__name__,"message":str(exc)}]}
        (folder/"case_state.json").write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
        (folder/"report.md").write_text(root_scout.markdown(state),encoding="utf-8")
        progress=folder/"case_state.inprogress.json"
        if progress.exists(): progress.unlink()
    return out


def classify_scope(state,category):
    """Describe finite trusted-test coverage without inferring semantic success from model prose."""
    if not state:
        return {"functional_scope_status":"unresolved","scope_reason":"missing_or_mismatched_result",
                "protocol_status":"unknown","infrastructure_status":"unknown","resource_status":"unverified",
                "abstention_status":"not_assessed","diagnostic_failure_count":0,"formal_trusted_failure_count":0}
    stop=state.get("stop_reason","unknown"); cov=state.get("coverage",{}); evidence={e.get("id"):e for e in state.get("evidence",[])}
    failures=cov.get("execution_failures",[])
    diagnostic=[f for f in failures if f.get("profile")=="diagnostic"]
    formal_trusted=[f for f in failures if f.get("profile")=="formal" and evidence.get(f.get("evidence_id"),{}).get("trusted_expected") is True]
    trusted=[e for e in state.get("evidence",[]) if e.get("trusted_expected") is True]
    trusted_executed=[e for e in trusted if isinstance(e.get("result"),dict)]
    trusted_mismatches=[e for e in trusted_executed if e["result"].get("status")=="ok" and e["result"].get("matched") is False]
    trusted_blocked=[e for e in trusted_executed if e["result"].get("status")!="ok"]
    available=int(cov.get("trusted_tests_available",len(trusted)) or 0)
    if trusted_mismatches: trusted_status="mismatch_observed"
    elif trusted_blocked: trusted_status="execution_blocked"
    elif available>0 and len(trusted_executed)>=available: trusted_status="no_failure_observed"
    else: trusted_status="insufficient"
    calls=state.get("budget",{}).get("calls",[]); failed_calls=[c for c in calls if c.get("status")=="failed"]
    error_classes={c.get("error_class") for c in failed_calls}
    legacy_types={c.get("error") for c in failed_calls if not c.get("error_class")}
    if "wall_limit" in error_classes: protocol_status="wall_limit"
    elif "model_call_timeout" in error_classes: protocol_status="model_call_timeout"
    elif "transport_failed" in error_classes: protocol_status="transport_failed"
    elif "response_parse_failed" in error_classes: protocol_status="response_parse_failed"
    elif "schema_validation_failed" in error_classes: protocol_status="schema_validation_failed"
    elif "model_response_failed" in error_classes: protocol_status="model_response_failed"
    elif legacy_types & {"URLError","HTTPError","TimeoutError","ConnectionError","OSError"}: protocol_status="transport_failed"
    elif "JSONDecodeError" in legacy_types: protocol_status="response_parse_failed"
    elif legacy_types: protocol_status="failed"
    elif failed_calls: protocol_status="model_call_failed"
    elif state.get("analysis_errors"): protocol_status="controller_or_phase_failed"
    elif any(c.get("status")=="rejected" and c.get("error")=="wall_budget_exhausted_before_call" for c in calls): protocol_status="wall_admission_rejected"
    elif any(c.get("status")=="rejected" and c.get("error")=="token_budget_exhausted_before_call_reserved_report" for c in calls): protocol_status="token_admission_rejected"
    elif any(c.get("status")=="rejected" for c in calls): protocol_status="budget_rejected"
    else: protocol_status="ok" if calls else "not_recorded"
    runtime=state.get("run_configuration",{}).get("runtime",{})
    infra_blocked=runtime.get("available") is False or any(f.get("status") in ("environment_blocked","docker_error","image_not_found") for f in formal_trusted)
    suspected=state.get("suspected_failures",[])
    known=set(cov.get("known_explained_evidence_ids",[]))
    open_e1=[x.get("evidence_id") for x in suspected if x.get("evidence_id") not in known]
    roots=state.get("roots",[])
    residual_done=cov.get("residual_status") in ("complete","not_applicable")
    findings_supported=bool(roots) and all(r.get("intervention",{}).get("status") in ("supported","jointly_supported","interaction_confounded") for r in roots)
    if stop=="no_bug_found_within_scope" and residual_done and not formal_trusted and not roots and not open_e1:
        functional="no_bug_found_within_finite_scope"; reason="all available trusted tests and bounded audit completed without an accepted root"
    elif stop=="completed_with_evidence" and residual_done and findings_supported and not formal_trusted and not open_e1:
        functional="verified_finite_scope_with_findings"; reason="accepted findings have finite intervention support and no open trusted E1 remains"
    else:
        functional="unresolved"; reason="stop, trusted execution, residual audit, E1, or intervention evidence leaves an open obligation"
    abstention="expected_abstention" if category=="missing_spec" and not roots and stop=="oracle_unresolved" else "abstention_mismatch" if category=="missing_spec" else "not_applicable"
    return {"functional_scope_status":functional,"scope_reason":reason,
            "protocol_status":protocol_status,"protocol_errors":[{"phase":c.get("phase"),"type":c.get("error"),"class":c.get("error_class")} for c in failed_calls],
            "infrastructure_status":"blocked" if infra_blocked else "available_for_recorded_runs" if runtime.get("available") is True else "unknown",
            "resource_status":"unverified","resource_reason":"No judge time limit or independent performance validation is recorded.",
            "abstention_status":abstention,"diagnostic_failure_count":len(diagnostic),
            "trusted_test_status":trusted_status,"trusted_test_count":available,"trusted_test_executed":len(trusted_executed),
            "trusted_mismatch_count":len(trusted_mismatches),"trusted_execution_blocked_count":len(trusted_blocked),
            "formal_trusted_failure_count":len(formal_trusted),"open_e1_ids":open_e1,
            "execution_failures":failures}


def score(dataset, states, reviews=None):
    reviews=reviews or {}
    defect_count=tp=fp=fn=exact=clean=clean_fp=resolved=0
    token_total=confirmed_token_total=unknown_reserved_total=latency_total=0.0; token_split_available=True; stop_counts={}; results=[]; root_total=0
    for case in dataset:
        case_id=case["case_id"]; expected=case["expected_roots"]; state=states.get(case_id)
        request_mismatch=bool(state and state.get("request_snapshot")!=case["request"])
        if request_mismatch: state=None
        roots=(state or {}).get("roots",[]); evidence={e.get("id"):e for e in (state or {}).get("evidence",[])}
        pred_matches=[]; matched_preds=set(); matched_labels=set(); method="missing_result"
        reviewed=case_id in reviews; review=reviews.get(case_id) or {}
        if reviewed and (not isinstance(review.get("reviewer"),str) or not review["reviewer"].strip() or not isinstance(review.get("matches"),list)):
            raise ValueError(f"manual review for {case_id} requires reviewer and an explicit matches array")
        if state:
            budget=state.get("budget",{}); token_total+=float(budget.get("tokens_total",0) or 0); latency_total+=float(budget.get("wall_seconds",0) or 0)
            if "confirmed_usage_tokens" in budget and "reserved_unknown_tokens" in budget:
                confirmed_token_total+=float(budget.get("confirmed_usage_tokens",0) or 0)
                unknown_reserved_total+=float(budget.get("reserved_unknown_tokens",0) or 0)
            else: token_split_available=False
            stop=state.get("stop_reason","unknown"); stop_counts[stop]=stop_counts.get(stop,0)+1
            scope=classify_scope(state,case.get("category"))
            resolved+=scope["functional_scope_status"]!="unresolved"
            method="human_review" if reviewed else "rule_proxy_line_and_trigger"
        else: stop="result_request_mismatch" if request_mismatch else "missing_result"; stop_counts[stop]=stop_counts.get(stop,0)+1; scope=classify_scope(None,case.get("category"))
        if reviewed and state:
            for pair in review.get("matches",[]):
                pi=next((i for i,r in enumerate(roots) if r.get("id")==pair.get("predicted_root_id")),None)
                ei=next((i for i,r in enumerate(expected) if r["frozen_id"]==pair.get("frozen_root_id")),None)
                if pi is not None and ei is not None and pi not in matched_preds and ei not in matched_labels:
                    matched_preds.add(pi); matched_labels.add(ei); pred_matches.append({"predicted":roots[pi].get("id"),"frozen_id":expected[ei]["frozen_id"],"method":method})
        elif state:
            for ei,label in enumerate(expected):
                for pi,pred in enumerate(roots):
                    if pi in matched_preds: continue
                    if pred.get("original_location",{}).get("line")!=label["source_line"]: continue
                    if any(evidence.get(eid,{}).get("input")==label["legal_trigger_input"] for eid in pred.get("evidence_ids",[])):
                        matched_preds.add(pi); matched_labels.add(ei)
                        pred_matches.append({"predicted":pred.get("id"),"frozen_id":label["frozen_id"],"method":method}); break
        case_tp=len(matched_labels); case_fp=max(0,len(roots)-len(matched_preds)); case_fn=max(0,len(expected)-case_tp)
        tp+=case_tp; fp+=case_fp; fn+=case_fn; root_total+=len(expected)
        is_defect=bool(expected)
        if is_defect:
            defect_count+=1; exact+=case_tp==len(expected) and case_fp==0
        if case["category"]=="clean":
            clean+=1; clean_fp+=bool(roots)
        results.append({"case_id":case_id,"category":case["category"],"stop_reason":stop,"matched":pred_matches,
                        "predicted_roots":len(roots),"expected_roots":len(expected),"TP":case_tp,"FP":case_fp,"FN":case_fn,
                        "all_root_exact":bool(is_defect and case_tp==len(expected) and case_fp==0),"match_method":method,
                        "reviewer":review.get("reviewer") if reviewed else None,
                        "confirmed_usage_tokens":(state.get("budget",{}).get("confirmed_usage_tokens") if state and "confirmed_usage_tokens" in state.get("budget",{}) else None),
                        "reserved_unknown_tokens":(state.get("budget",{}).get("reserved_unknown_tokens") if state and "reserved_unknown_tokens" in state.get("budget",{}) else None),
                        "budget_exit_reason":(state.get("budget",{}).get("exit_reason") if state else None),**scope})
    n=len(dataset)
    mismatches=[cid for cid,result_state in states.items() if any(x["case_id"]==cid and result_state.get("request_snapshot")!=x["request"] for x in dataset)]
    observed=sum(case["case_id"] in states and case["case_id"] not in mismatches for case in dataset)
    by_category={}; cases_by_id={x["case_id"]:x for x in dataset}
    for row in results:
        group=by_category.setdefault(row["category"],{"count":0,"tokens":0,"confirmed_usage_tokens":0,"reserved_unknown_tokens":0,"token_split_available":True,"wall_seconds":0.0,"stop_reasons":{},"functional_scope_statuses":{},"protocol_failures":0,"abstentions":{}})
        metric_state=states.get(row["case_id"])
        if metric_state and metric_state.get("request_snapshot")!=cases_by_id[row["case_id"]]["request"]: metric_state=None
        group["count"]+=1; group["tokens"]+=int((metric_state or {}).get("budget",{}).get("tokens_total",0) or 0)
        group["wall_seconds"]+=float((metric_state or {}).get("budget",{}).get("wall_seconds",0) or 0)
        metric_budget=(metric_state or {}).get("budget",{})
        if "confirmed_usage_tokens" in metric_budget and "reserved_unknown_tokens" in metric_budget:
            group["confirmed_usage_tokens"]+=int(metric_budget.get("confirmed_usage_tokens",0) or 0)
            group["reserved_unknown_tokens"]+=int(metric_budget.get("reserved_unknown_tokens",0) or 0)
        else: group["token_split_available"]=False
        for field,key in (("stop_reason","stop_reasons"),("functional_scope_status","functional_scope_statuses"),("abstention_status","abstentions")):
            value=row.get(field,"unknown"); group[key][value]=group[key].get(value,0)+1
        group["protocol_failures"]+=row.get("protocol_status") not in ("ok","not_recorded","budget_rejected","token_admission_rejected","wall_admission_rejected")
    return {"dataset_size":n,"results_available":observed,"missing_results":n-observed,"request_mismatches":mismatches,"TP":tp,"FP":fp,"FN":fn,
            "root_precision":tp/(tp+fp) if tp+fp else None,"root_recall":tp/root_total if root_total else None,
            "all_root_exact":exact/defect_count if defect_count else None,"all_root_exact_numerator":exact,"all_root_exact_denominator":defect_count,
            "clean_fpr":clean_fp/clean if clean else None,"clean_false_positives":clean_fp,"clean_samples":clean,
            "unresolved_count":n-resolved,"resolved_coverage":resolved/n if n else None,"finite_functional_scope_count":resolved,
            "resource_status":"unverified","resource_reason":"No judge time limit or independent performance validation is recorded.","category_summary":by_category,"stop_reasons":stop_counts,
            "total_tokens":int(token_total),"total_tokens_basis":"confirmed usage plus reserved unknown; legacy field retained",
            "confirmed_usage_tokens":int(confirmed_token_total) if token_split_available else None,
            "reserved_unknown_tokens":int(unknown_reserved_total) if token_split_available else None,
            "token_split_available":token_split_available,
            "mean_tokens_per_dataset_sample_including_missing_as_zero":token_total/n if n else None,
            "mean_tokens_observed_results":token_total/observed if observed else None,
            "total_reported_wall_seconds":round(latency_total,3),"mean_wall_seconds_per_dataset_sample_including_missing_as_zero":latency_total/n if n else None,
            "mean_wall_seconds_observed_results":latency_total/observed if observed else None,
            "matching_method":"exact source-line + linked legal trigger-input rule proxy unless manual review map supplied; not general semantic equivalence",
            "cases":results}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest="command",required=True)
    w=subs.add_parser("write-samples"); w.add_argument("--out",default=str(DATASET))
    s=subs.add_parser("score"); s.add_argument("--dataset",default=str(DATASET)); s.add_argument("--results",nargs="*",default=[]); s.add_argument("--reviews"); s.add_argument("--out")
    r=subs.add_parser("run"); r.add_argument("--dataset",default=str(DATASET)); r.add_argument("--out",default="evaluation-runs"); r.add_argument("--provider",choices=("model","replay"),default="replay")
    args=parser.parse_args()
    if args.command=="write-samples":
        print(f"wrote {write_dataset(args.out)} annotated fixtures to {args.out}"); return
    if args.command=="run":
        out=run_dataset(read_jsonl(args.dataset),args.out,args.provider); print(out); return
    review_map={}
    if args.reviews:
        review_map={row["case_id"]:row for row in read_raw_jsonl(args.reviews)}
    report=score(read_jsonl(args.dataset),load_states(args.results),review_map)
    output=json.dumps(report,ensure_ascii=False,indent=2)
    if args.out: pathlib.Path(args.out).write_text(output+"\n",encoding="utf-8")
    print(output)

if __name__=="__main__": main()
