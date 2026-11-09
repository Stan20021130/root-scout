"""Two deliberately narrow, deterministic small-input oracles."""
from __future__ import annotations

import hashlib
from functools import lru_cache
import random
from pathlib import Path

ORACLE_VERSION = "root-scout-oracles-v1"
ORACLE_IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
# Historical oracle code is retained for replay verification only. New evidence
# always records the current implementation hash above.
LEGACY_ORACLE_IMPLEMENTATION_SHA256S = {
    "451bbb948fd595408464000783a074b39e24f9dc344a0157fe433d9a22bfbdf0": {"range_sum.v1", "max.v1", "range_min.v1"}
}


def implementation_sha256_known(value):
    return value == ORACLE_IMPLEMENTATION_SHA256 or value in LEGACY_ORACLE_IMPLEMENTATION_SHA256S


def implementation_sha256_supported(value, kind=None):
    if value == ORACLE_IMPLEMENTATION_SHA256: return True
    return isinstance(kind,str) and kind in LEGACY_ORACLE_IMPLEMENTATION_SHA256S.get(value,set())


CONTRACTS = {
    "range_sum.v1": {
        "statement_anchor": "Given an integer array, answer each 1-based closed-interval sum query [l,r].",
        "semantics": "sum array elements at 1-based indices l through r inclusive",
        "input_format": "n q; n integer values; then q pairs l r",
        "generator": "range-sum-small-v1",
    },
    "max.v1": {
        "statement_anchor": "Given n integers, output their maximum.",
        "semantics": "output the maximum of the n input integers",
        "input_format": "n; n integer values",
        "generator": "max-small-v1",
    },
    "range_min.v1": {
        "statement_anchor": "Given an integer array, answer each 1-based closed-interval minimum query [l,r].",
        "semantics": "minimum array element at 1-based indices l through r inclusive",
        "input_format": "n q; n integer values; then q pairs l r",
        "generator": "range-min-small-v1",
    },
    "increasing_xor.v1": {
        "statement_anchor": "find all integers x ≥ 0 such that there exists an increasing subsequence of the array a, in which the bitwise XOR of numbers is equal to x.",
        "semantics": "output every XOR value attainable by a strictly increasing subsequence, including the empty subsequence value 0, in increasing order",
        "input_format": "n; n integer values in [0,5000], whitespace separated",
        "generator": "increasing-xor-small-v1",
    },
    "two_sorts.v1": {
        "statement_anchor": "Integers from 1 to n (inclusive) were sorted lexicographically (considering integers as strings). As a result, array a_1, a_2, ..., a_n was obtained.",
        "semantics": "sort integers 1..n by decimal-string lexicographic order, then output sum((i-a_i) mod 998244353) mod 1000000007 using 1-based i",
        "input_format": "one integer n",
        "generator": "two-sorts-bounded-v1",
    },
}


def validate_contract(statement, contract):
    if not isinstance(contract, dict):
        return None, "tool_contract_missing_or_malformed"
    kind = contract.get("kind")
    spec = CONTRACTS.get(kind)
    if not spec:
        return None, "unsupported_tool_contract"
    if contract.get("version") != 1:
        return None, "unsupported_tool_contract_version"
    if contract.get("caller_attested") is not True or not isinstance(contract.get("reviewed_by"), str) or not contract["reviewed_by"].strip() or contract["reviewed_by"].strip().casefold()=="model":
        return None, "caller_review_attestation_required"
    statement_hash=hashlib.sha256(statement.encode("utf-8")).hexdigest()
    if contract.get("statement_sha256") != statement_hash:
        return None, "statement_hash_binding_mismatch"
    quote = contract.get("statement_quote")
    if not isinstance(quote, str) or not quote or quote not in statement or quote != spec["statement_anchor"]:
        return None, "statement_binding_mismatch"
    if contract.get("semantics") != spec["semantics"] or contract.get("input_format") != spec["input_format"]:
        return None, "caller_semantic_declaration_mismatch"
    domain = contract.get("domain")
    domain_quote = contract.get("domain_quote")
    if not isinstance(domain, dict) or not isinstance(domain_quote, str) or not domain_quote or domain_quote not in statement:
        return None, "domain_source_missing_or_mismatch"
    domain_quotes=contract.get("domain_quotes",[domain_quote])
    if not isinstance(domain_quotes,list) or not domain_quotes or any(not isinstance(q,str) or not q or q not in statement for q in domain_quotes):
        return None, "domain_sources_missing_or_mismatch"
    if domain_quote not in domain_quotes: return None, "primary_domain_source_not_listed"
    required = ("n_min", "n_max") + (() if kind == "two_sorts.v1" else ("value_min", "value_max"))
    if any(type(domain.get(k)) is not int for k in required):
        return None, "domain_bounds_malformed"
    if domain["n_min"] < 1 or domain["n_max"] < domain["n_min"] or domain["n_max"] > 200000 or (kind != "two_sorts.v1" and domain["value_min"] > domain["value_max"]):
        return None, "domain_does_not_cover_small_oracle_witnesses"
    if kind=="increasing_xor.v1" and (domain["n_max"]>8 or domain["value_min"]<0 or domain["value_max"]>5000):
        return None,"domain_exceeds_increasing_xor_oracle"
    if kind=="two_sorts.v1" and domain["n_max"]>1000:
        return None,"domain_exceeds_two_sorts_oracle"
    if kind in ("range_sum.v1", "range_min.v1") and (type(domain.get("q_min")) is not int or type(domain.get("q_max")) is not int or domain["q_min"] < 1 or domain["q_min"] > domain["q_max"] or domain["q_max"] > 200000):
        return None, "query_domain_bounds_malformed"
    canonical={**spec,"kind":kind,"domain":dict(domain),"statement_quote":quote,"domain_quote":domain_quote,
               "reviewed_by":contract["reviewed_by"],"caller_attested":True,"statement_sha256":statement_hash,"version":1}
    if "domain_quotes" in contract: canonical["domain_quotes"]=list(domain_quotes)
    return canonical,None


def evaluate(kind, input_text, domain):
    """Return trusted output, or UNKNOWN outside the validated small domain."""
    if kind not in CONTRACTS or not isinstance(input_text, str):
        return {"status": "unknown", "reason": "unsupported_or_malformed"}
    try:
        lines = input_text.splitlines()
        if not lines or not lines[0].strip():
            raise ValueError("missing header")
        header = [int(x) for x in lines[0].split()]
        if kind == "two_sorts.v1":
            if len(header) != 1 or any(line.split() for line in lines[1:]): raise ValueError("expected one integer n")
            n=header[0]
            if not domain["n_min"] <= n <= min(domain["n_max"],1000):
                return {"status":"unknown","reason":"outside_declared_oracle_domain"}
            ordered=sorted(range(1,n+1),key=str)
            total=sum((i-value)%998244353 for i,value in enumerate(ordered,1))%1000000007
            return {"status":"known","expected":str(total)+"\n","valid":True}
        if kind == "increasing_xor.v1":
            if len(header) != 1: raise ValueError("expected n")
            n=header[0]
            if not domain["n_min"] <= n <= min(domain["n_max"],8):
                return {"status":"unknown","reason":"outside_declared_oracle_domain"}
            values=[int(x) for line in lines[1:] for x in line.split()]
            if len(values)!=n: raise ValueError("wrong token count")
            if any(not domain["value_min"] <= x <= domain["value_max"] for x in values):
                return {"status":"unknown","reason":"value_outside_declared_domain"}
            # Tiny-domain production oracle: enumerate all subsequences and
            # retain only strictly increasing selections.
            attainable={0}
            for mask in range(1,1<<n):
                selected=[values[i] for i in range(n) if mask & (1<<i)]
                if all(selected[i-1]<selected[i] for i in range(1,len(selected))):
                    value=0
                    for x in selected: value ^= x
                    attainable.add(value)
            ordered=sorted(attainable)
            return {"status":"known","expected":str(len(ordered))+"\n"+" ".join(map(str,ordered))+"\n","valid":True}
        if kind in ("range_sum.v1", "range_min.v1"):
            if len(header) != 2: raise ValueError("expected n q")
            n, q = header
            if not domain["n_min"] <= n <= min(domain["n_max"], 8) or not domain["q_min"] <= q <= min(domain["q_max"], 8):
                return {"status": "unknown", "reason": "outside_declared_oracle_domain"}
            tokens = [int(x) for line in lines[1:] for x in line.split()]
            if len(tokens) != n + 2*q: raise ValueError("wrong token count")
            values, query_tokens = tokens[:n], tokens[n:]
            if any(not domain["value_min"] <= x <= domain["value_max"] for x in values):
                return {"status": "unknown", "reason": "value_outside_declared_domain"}
            answers=[]
            for i in range(0, len(query_tokens), 2):
                left, right=query_tokens[i:i+2]
                if not 1 <= left <= right <= n: raise ValueError("invalid closed interval")
                window=values[left-1:right]
                answers.append(sum(window) if kind == "range_sum.v1" else min(window))
            return {"status":"known","expected":"\n".join(map(str, answers))+"\n","valid":True}
        if len(header) != 1: raise ValueError("expected n")
        n=header[0]
        if not domain["n_min"] <= n <= min(domain["n_max"], 8):
            return {"status":"unknown","reason":"outside_declared_oracle_domain"}
        values=[int(x) for line in lines[1:] for x in line.split()]
        if len(values)!=n: raise ValueError("wrong token count")
        if any(not domain["value_min"] <= x <= domain["value_max"] for x in values):
            return {"status":"unknown","reason":"value_outside_declared_domain"}
        return {"status":"known","expected":str(max(values))+"\n","valid":True}
    except (ValueError, IndexError):
        return {"status":"unknown","reason":"input_invalid_or_ambiguous"}


@lru_cache(maxsize=None)
def self_check(kind):
    """Compare the production oracle with a separately written check mechanism."""
    if kind == "range_sum.v1":
        cases=[("3 3\n-2 0 5\n1 1\n1 3\n2 3\n", "-2\n3\n5\n"),
               ("1 1\n7\n1 1\n", "7\n")]
        for inp, expected in cases:
            got=evaluate(kind, inp, {"n_min":1,"n_max":8,"q_min":1,"q_max":8,"value_min":-100,"value_max":100})
            # independent prefix-sum formulation and hand-checked expected fixture
            lines=inp.splitlines(); n,q=map(int,lines[0].split()); a=list(map(int,lines[1].split())); prefix=[0]
            for x in a: prefix.append(prefix[-1]+x)
            values=[str(prefix[int(x.split()[1])]-prefix[int(x.split()[0])-1]) for x in lines[2:]]
            if got.get("expected")!="\n".join(values)+"\n" or got.get("expected")!=expected:
                return False
        return True
    if kind == "max.v1":
        cases=[("3\n-9 -2 -7\n","-2\n"),("1\n5\n","5\n")]
        for inp, expected in cases:
            got=evaluate(kind, inp, {"n_min":1,"n_max":8,"value_min":-100,"value_max":100})
            values=list(map(int,inp.splitlines()[1].split()))
            if got.get("expected")!=str(sorted(values)[-1])+"\n" or got.get("expected")!=expected:
                return False
        return True
    if kind == "range_min.v1":
        cases=[("3 3\n-2 0 5\n1 1\n1 3\n2 3\n", "-2\n-2\n0\n"),
               ("1 1\n7\n1 1\n", "7\n"),
               ("4 2\n3 3 3 1\n1 4\n2 2\n", "1\n3\n")]
        for inp, expected in cases:
            got=evaluate(kind, inp, {"n_min":1,"n_max":8,"q_min":1,"q_max":8,"value_min":-100,"value_max":100})
            # independent formulation: sort each closed window and take its first element
            lines=inp.splitlines(); a=list(map(int,lines[1].split()))
            values=[str(sorted(a[int(x.split()[0])-1:int(x.split()[1])])[0]) for x in lines[2:]]
            if got.get("expected")!="\n".join(values)+"\n" or got.get("expected")!=expected:
                return False
        return True
    if kind == "increasing_xor.v1":
        domain={"n_min":1,"n_max":8,"value_min":0,"value_max":5000}
        rng=random.Random(20261029)
        vectors=[]
        for n in range(1,9):
            vectors.extend(([0]*n,list(range(n)),list(reversed(range(n))),[i//2 for i in range(n)],
                            [5000 if i%2 else i for i in range(n)]))
        vectors.extend([[rng.randrange(5001) for _ in range(n)] for n in range(1,9) for _ in range(4)])
        for values in vectors:
            inp=str(len(values))+"\n"+" ".join(map(str,values))+"\n"
            got=evaluate(kind,inp,domain)
            # Independent state DP over (last value, xor), not subsequence masks.
            states=[(-1,0)]
            for value in values:
                states += [(value,xor^value) for last,xor in states[:] if last<value]
            expected=sorted({xor for _last,xor in states})
            text=str(len(expected))+"\n"+" ".join(map(str,expected))+"\n"
            if got.get("status")!="known" or got.get("expected")!=text: return False
        return True
    if kind == "two_sorts.v1":
        domain={"n_min":1,"n_max":1000}
        def prefix_order(n):
            out=[]
            def visit(prefix):
                if prefix:
                    value=int(prefix)
                    if value>n: return
                    out.append(value)
                for digit in "0123456789":
                    child=prefix+digit
                    if child and child[0]!="0" and int(child)<=n: visit(child)
            for first in "123456789":
                if int(first)<=n: visit(first)
            return out
        checks=set(range(1,1001))|{3,12,21}
        for n in sorted(checks):
            ordered=prefix_order(n)
            if ordered!=sorted(range(1,n+1),key=str): return False
            expected=str(sum((i-value)%998244353 for i,value in enumerate(ordered,1))%1000000007)+"\n"
            got=evaluate(kind,str(n)+"\n",domain)
            if got.get("status")!="known" or got.get("expected")!=expected: return False
        for n,hand in ((3,"0\n"),(12,"994733045\n"),(21,"978932159\n")):
            if evaluate(kind,str(n)+"\n",domain).get("expected")!=hand: return False
        return True
    return False


def generate(contract, batch="initial"):
    """Generate a fixed handful of legal boundary cases; no random or model output."""
    if not self_check(contract["kind"]):
        return []
    d=contract["domain"]
    if contract["kind"]=="increasing_xor.v1":
        low,high=d["value_min"],d["value_max"]
        zero=min(high,max(low,0)); one=min(high,max(low,1))
        if batch=="initial":
            vectors=[[high],[zero,zero],[zero,one,min(high,one+1),min(high,one+2)],
                     [high,max(low,high-1),max(low,high-2),low,low],
                     [zero,high,one,high,min(high,one+1),high,low,high]]
            rng=random.Random(20261029)
            vectors.extend([[rng.randint(low,high) for _ in range(n)] for n in (3,6,8)])
        else:
            rng=random.Random(20261030)
            vectors=[[rng.randint(low,high) for _ in range(n)] for n in (2,4,5,6,7,8,3,8)]
        cases=_generated_cases(contract,batch,[v for v in vectors if d["n_min"]<=len(v)<=min(d["n_max"],8)])
        if batch=="residual":
            prior={(x["input"],x["expected"]) for x in generate(contract,"initial")}
            cases=[x for x in cases if (x["input"],x["expected"]) not in prior]
        for index,case in enumerate(cases,1): case["oracle_case_id"]=batch+"-"+str(index)
        return cases
    if contract["kind"]=="two_sorts.v1":
        ns=([1,9,10,99,100,101,500,999] if batch=="initial"
            else [2,19,20,21,199,200,201,1000])
        cases=_generated_cases(contract,batch,[n for n in ns if d["n_min"]<=n<=min(d["n_max"],1000)])
        if batch=="residual":
            prior={(x["input"],x["expected"]) for x in generate(contract,"initial")}
            cases=[x for x in cases if (x["input"],x["expected"]) not in prior]
        for index,case in enumerate(cases,1): case["oracle_case_id"]=batch+"-"+str(index)
        return cases
    sizes=[n for n in (1,2,4,8) if d["n_min"]<=n<=min(d["n_max"],8)]
    if not sizes and d["n_min"]<=min(d["n_max"],8): sizes=[d["n_min"]]
    if not sizes: return []
    low,high=d["value_min"],d["value_max"]
    zero=min(high,max(low,0)); mid=min(high,max(low,1))
    if contract["kind"] in ("range_sum.v1", "range_min.v1"):
        vectors=[]
        selected=sizes if batch=="initial" else sizes[-2:]
        for j,n in enumerate(selected):
            if batch=="initial":
                if contract["kind"] == "range_sum.v1":
                    values=([zero]*n if j==0 else [high,low]+[mid]*max(0,n-2) if j==1 else [high]*n if j==2 else [low]+[high]*max(0,n-1))
                else:
                    values=([mid]*n if j==0 else [low,high]+[mid]*max(0,n-2) if j==1 else [high]*n if j==2 else [low]+[high]*max(0,n-1))
            else: values=[low]+[high]*max(0,n-1)
            pairs=[(1,1),(n,n),(1,n),(max(1,n//2),n)]
            if batch=="residual": pairs=[(n,n),(1,max(1,n-1)),(1,n),(max(1,n//2),n)]
            q=max(d["q_min"],min(d["q_max"],min(8,len(pairs))))
            if q>8: return []
            pairs=(pairs+[pairs[-1]]*q)[:q]
            vectors.append(f"{n} {q}\n"+" ".join(map(str,values))+"\n"+"".join(f"{l} {r}\n" for l,r in pairs))
    else:
        vectors=[]
        selected=sizes if batch=="initial" else sizes[-2:]
        for j,n in enumerate(selected):
            pattern=([zero]*n if j==0 else ([low]*n if j==1 else [low]+[high]+[mid]*max(0,n-2) if j==2 else [mid]*max(0,n-1)+[high])) if batch=="initial" else ([high]+[low]*max(0,n-1))
            vectors.append(f"{n}\n"+" ".join(map(str,pattern[:n]))+"\n")
    out=[]; seen=set()
    for index, input_text in enumerate(vectors, 1):
        answer=evaluate(contract["kind"], input_text, contract["domain"])
        if answer["status"] != "known":
            continue
        pair=(input_text,answer["expected"])
        if pair in seen: continue
        seen.add(pair)
        out.append({"input":input_text,"expected":answer["expected"],"trusted":True,
                    "expected_source":ORACLE_VERSION+":"+contract["kind"],"legal_input":True,
                    "legal_input_source":contract["domain_quote"],"source_kind":"builtin_oracle:"+contract["kind"],
                    "oracle_version":ORACLE_VERSION,"oracle_contract_sha256":contract["contract_sha256"],
                    "oracle_implementation_sha256":ORACLE_IMPLEMENTATION_SHA256,
                    "oracle_case_id":batch+"-"+str(index),"oracle_validated":True})
    if batch=="residual":
        prior={(x["input"],x["expected"]) for x in generate(contract,"initial")}
        out=[x for x in out if (x["input"],x["expected"]) not in prior]
    for index,case in enumerate(out,1): case["oracle_case_id"]=batch+"-"+str(index)
    return out


def _generated_cases(contract,batch,items):
    out=[]; seen=set(); kind=contract["kind"]
    for index,item in enumerate(items,1):
        input_text=(str(item)+"\n" if kind=="two_sorts.v1" else str(len(item))+"\n"+" ".join(map(str,item))+"\n")
        answer=evaluate(kind,input_text,contract["domain"])
        if answer.get("status")!="known" or (input_text,answer["expected"]) in seen: continue
        seen.add((input_text,answer["expected"]))
        out.append({"input":input_text,"expected":answer["expected"],"trusted":True,
                    "expected_source":ORACLE_VERSION+":"+kind,"legal_input":True,
                    "legal_input_source":"; ".join(contract.get("domain_quotes",[contract["domain_quote"]])),"source_kind":"builtin_oracle:"+kind,
                    "oracle_version":ORACLE_VERSION,"oracle_contract_sha256":contract["contract_sha256"],
                    "oracle_implementation_sha256":ORACLE_IMPLEMENTATION_SHA256,
                    "oracle_case_id":batch+"-"+str(index),"oracle_validated":True})
    return out
