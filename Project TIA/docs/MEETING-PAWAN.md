# Meeting with Pawan: TIA use cases and graph structure

Source: `Pawan meet.m4a` (25 min 25 s). Machine-transcribed with Whisper (`small` model, English). There are no speaker labels, and
names and product terms may be misheard: corrections are given in [brackets]. Pawan is the reviewer, and "you" means the TIA developer.

---

## 1. What Pawan asked for

**The principle.** Fix the graph representation once. Then, for each use case, show how that same representation answers it. Change
the representation only cautiously, and only when a use case shows it cannot be met. Do not reshape the graph for every use case
(10:53-11:54, 15:03-15:22).

| # | Ask | Timestamp |
|---|-----|-----------|
| 0 | **Step 0: say how the code graph is stored and connected.** Say which database it uses, how data is extracted, and how the graph is built. Store it in **Neo4j** (the new Knowledge Fabric 2.5 instance), not a separate graph DB, because TIA will be integrated into KF 2.5. | 11:56-12:31, 24:33-24:47 |
| 0a | **Link the code graph to the KF ontology**: Domain > Product line > Function > Sub-function > Requirement / Process / Rule. Rules extracted from code (for example "minimum balance 80") link to KF **rule** nodes. API endpoints link to KF **system extracts**. | 12:32-13:40 |
| 0b | **Test cases link to sub-functions.** An API links to a sub-function and a test case links to a sub-function, so a change reaches the related test cases through the ontology. | 13:46-14:03, 23:32 |
| 1 | **Use case 1: blast radius and regression test selection.** Use a small sample app, the payments one (Fineract comes later). Keep a **main branch** and a **feature branch** with one change. Put about **10 functional test cases** in the graph. Comparing the branches should say "these are the 3-4 of your 10 test cases to regression-test", on top of the new tests the tester writes in Pulse for the change itself. Test cases in the graph make it a *Knowledge Fabric demo*; outside the graph makes it an *agent demo*. Either works, and Azure test cases are also acceptable. | 05:43-07:22, 09:22-10:06, 23:05-24:18 |
| 1a | **Explain it in the tester's terms.** The tester only hears "change deployed in Dev". Pulse should tell them what the developer changed beyond the obvious, for example "they also added a new environment variable that other methods use, so test these too". | 03:44-05:41 |
| 1b | **Skip developer-level coverage.** Unit-test pass rate and code coverage gates are already solved in DevSecOps pipelines. **Functional coverage** is what matters. | 02:14-03:32 |
| 2 | **Use case 2: automation.** The graph holds **Application > Screen (page) > Element > locator attributes** (unique id, CSS path, XPath). Given a flow "screen A to screen B", an agent can then write an automation script from the graph. | 17:59-19:10, 22:08-22:50 |
| 3 | **Use case 3: API testing.** The graph holds **API > Method > Request body / Response / Expected response**, plus field **definitions** (for example "customer ID, 15 characters"). A retriever can then generate API test cases, and Pulse can even call the API directly. | 19:36-22:08 |
| 4 | **Higher-level code view.** Skip "this class has 4 methods calling another class", which is extra noise. Show the **execution flow**, the **main entities**, the **controllers, models and views**: the design, not the call-level detail. | 15:40-17:40 |
| — | **Order:** step 0 (structure and how it connects), then use case 1 (blast radius and impact test cases), then automation, then API. | 24:47-25:08 |

---

## 2. Does TIA do this today?

Checked against the code in `Project TIA/tia/` on 15 Nov 2026.

| # | Ask | Status | What exists / what is missing |
|---|-----|--------|-------------------------------|
| 0 | Fixed, documented representation | **Partly** | The representation is fixed and documented ([ARCHITECTURE-AS-BUILT.md](ARCHITECTURE-AS-BUILT.md), [GRAPH-STORE.md](GRAPH-STORE.md)). Node types: service, function, type, schema, endpoint, callsite, test, topic, listener, event_schema, data. |
| 0 | Stored in Neo4j (KF 2.5) | **No** | The graph lives in **PostgreSQL** as a versioned JSON graph ([graphstore.py](../tia/graphstore.py)). The code has no Neo4j driver, Cypher or Bolt anywhere. |
| 0a | Linked to the KF ontology (rules, system extracts) | **No** | [kfadapter.py](../tia/kfadapter.py) only *answers* KF agents with a finished report in KF's `QueryResponse` shape. It writes nothing into KF's graph, and nothing links an endpoint to a system extract or a code rule to a KF rule. |
| 0b | Test cases linked to sub-functions | **No** | Written test cases ([testcases.py](../tia/testcases.py), `testcases/*.csv\|yaml\|json` in a repo) are matched to endpoints by text. They are not graph nodes, and no sub-function exists. |
| 1 | Main vs feature branch blast radius | **Yes** | `--base` / `--head`, merge-base, PR webhooks ([analysis_service.py](../tia/analysis_service.py)). The test scope lists every entry point the change reaches, with why and the change effect ([testscope.py](../tia/testscope.py)). |
| 1 | "These 4 of your 10 functional test cases" | **Built 15 Nov 2026** (simulated) | [regression.py](../tia/regression.py): every analysis reports *Regression-test N of your M test case(s)*. Each written case is selected (with the reached entry point, the change effect and why), not affected, unmapped (review by hand) or new in this change. The report, PR comment, report page, web UI, MCP `get_test_scope` and KF `QueryResponse` carry it. Demo: `tools/demo.py --landscape regression` (4 / 3 / 2 of 10). Matching is by the entry point or screen named in the case; matching through KF sub-functions waits on step 0. |
| 1a | Explained in the tester's terms | **Built 15 Nov 2026** (simulated) | [configkeys.py](../tia/configkeys.py): a configuration key changed in `.env`, `.properties`, YAML, INI, TOML or JSON reaches the functions that read it. A newly read environment variable is reported with where it is (not) set and which other functions read it. Values are never shown. |
| 2 | Application > Screen > Element > Locator in the graph | **Data yes, graph no** | [extract_ui.py](../tia/extract_ui.py) extracts pages, elements, and locators (test id / id / name / CSS / XPath / Playwright), mandatory flags and dropdown values ([automation.py](../tia/automation.py)). These are **computed report views**, not stored nodes, and there is no Application node. Nothing generates a script from a screen A to screen B flow. |
| 3 | API > Method > Request / Response / Definitions in the graph | **Data partly, graph partly** | Endpoint and schema nodes exist. Request fields with their rules (length, pattern, enum), valid and invalid values, a test matrix and error statuses are computed by [automation.py](../tia/automation.py), again as a view and not as nodes. **Expected-response examples** are not captured. Pulse cannot call an API from TIA's data. |
| 4 | Higher-level view: execution flow, entities, controllers/models | **Partly** | [graphview.py](../tia/graphview.py) gives service and endpoint levels, and [structure.py](../tia/structure.py) gives folder-based layers (controllers / services / models). There is **no execution-flow view** (entry point > controller > service > repository > table) and no entity/model summary. |

**In short:** the analysis behind use case 1 (blast radius from a branch diff) works. What Pawan actually asked for in this meeting
does not exist yet: TIA's graph **in KF's Neo4j, connected to the KF ontology**, with **test cases reached through sub-functions**. The
automation and API data is extracted but is not graph structure.

---

## 3. What we must do, in Pawan's order

### Step 0: storage and connection (do first)
1. **Write a Neo4j export/sync** (`tia/kfgraph.py`) that writes TIA's graph version into the KF 2.5 Neo4j instance. Use TIA labels
   (`:TIA_Service`, `:TIA_Endpoint`, `:TIA_Function`, `:TIA_Test`, `:TIA_Schema`), keep the version id on every node, and use idempotent `MERGE`.
   The bolt URI and credentials go through the existing secret store. Keep PostgreSQL as TIA's system of record, or decide with Pawan
   whether Neo4j becomes the record.
2. **Write the ontology links**, each with an evidence status (confirmed / inferred) and never guessed silently:
   - `(:TIA_Endpoint)-[:IMPLEMENTS]->(:SystemExtract)`, matched on method + path;
   - `(:TIA_Rule)-[:IMPLEMENTS]->(:Rule)` for rules extracted from code. TIA already finds data rules (filters/conditions) for rule gaps;
     it needs them stored as rule nodes;
   - `(:TestCase)-[:TESTS]->(:SubFunction)`, using KF's existing test-case nodes if KF has them.
3. **Write a one-page "how TIA is stored" note** for Pawan: DB, extraction, node and edge types, and the link to the ontology. He asked for
   this document demo, not a code demo.
4. **Get from the KF team:** the Neo4j 2.5 connection, and the exact labels and properties of SubFunction, Rule, SystemExtract and TestCase.
   *This blocks steps 1-2. We cannot guess KF's schema.*

### Use case 1: blast radius and regression test selection (items 5-7 built 15 Nov 2026, see the table above)
5. **Build the demo data:** add `testcases/` with about 10 functional test cases to the payments sample (or load them into Neo4j linked to
   sub-functions), plus a `feature/...` branch with one change.
6. **Add a "regression test selection" output:** for a base > feature diff, print "**N of M test cases to regression-test**" with one
   reason per selected case (changed function > endpoint > sub-function > test case) and the ones not selected. Use the
   sub-function route when the KF links exist, and the direct endpoint match otherwise.
7. **Detect config and env-var changes** (new or changed environment variables and config keys, and the code that reads them), so the
   "he also added an env variable" case is reported.

### Use case 2: automation (after use case 1)
8. **Store UI as graph nodes:** `Application > Screen > Element > Locator` from what `extract_ui.py` already extracts. Add an agent or
   retrieval path that, given "screen A to screen B", returns the screens, elements and best locators for script generation.

### Use case 3: API (after use case 2)
9. **Store API structure as nodes:** `Endpoint > Method > RequestBody > Field (definition: type, length, pattern, enum, mandatory)` and
   `Response > Field`. Capture **example / expected responses** from OpenAPI `example(s)` and from tests. Expose them so Pulse
   can build and send a request.

### Higher-level view
10. **Add an execution-flow and design view:** entry point > controller > service > repository > table, plus the main entities and models,
    without call-level detail.

---

## 4. Transcript

**[00:00]** Impact analysis create a blast radius, understand the blast radius and then from that you know  
**[00:07]** they put the relevant test cases and execute.  
**[00:10]** So, what was their use case?  
**[00:12]** Okay.  
**[00:13]** Which I found interesting.  
**[00:14]** So, everything happens in Github itself or whatever is the repository.  
**[00:27]** When you come again the use case, so I think you said that we have a code then there is  
**[00:33]** a PR change and there are already test cases done inside and we will make sure that whether  
**[00:39]** these test cases are sufficient or not, am I right?  
**[00:44]** Right.  
**[00:45]** Okay.  
**[00:46]** Not sufficient or not, it will either, it will actually identify the test cases which  
**[00:51]** are required to test the change.  
**[00:53]** Okay.  
**[00:54]** Regression test cases.  
**[00:56]** So, here I am choosing service and going for here.  
**[01:03]** Different project is it?  
**[01:06]** Because it is Fineract [Apache Fineract], it is a large 128 MB file.  
**[01:09]** So, we have all those impacts, incidents, incidents I do not know.  
**[01:14]** See, I have coded for incident but I do not have the file for incidents.  
**[01:20]** Okay.  
**[01:21]** Okay.  
**[01:22]** So, if you see, you have the ID, okay.  
**[01:29]** You have the Fineract [Apache Fineract], then it is coming from the source for Java, okay and it is a test  
**[01:37]** case for Java, for repo, for service and also the file.  
**[01:44]** Okay.  
**[01:45]** Where are you saying I will see three modes.  
**[01:52]** Okay.  
**[01:53]** Okay.  
**[01:54]** So, here we can see the downstream and if I go and hit, okay, here we have.  
**[02:13]** Yes.  
**[02:14]** If they have set the, you know, right quality gates, it is part of the deployment.  
**[02:29]** Yes, but this is the idea.  
**[02:31]** It will help to find, identify the gates if the gates are fixed properly or not.  
**[02:39]** Okay.  
**[02:40]** So, I am saying, even otherwise, anyway, if one or two is used and then if they have already  
**[02:48]** set the minimum pass percentage of unit test cases, 80% or 90% something like that, you  
**[02:53]** know, code coverage is 80%, 90%.  
**[02:56]** Then this problem will be solved there.  
**[03:00]** There is unit test case for 80% of the code, 90% of the code, right.  
**[03:06]** So, for each build, it will already be there in the DevSecOps pipeline itself, but  
**[03:16]** that is from a developer community standpoint, right.  
**[03:21]** So, that is a problem, that is a solved problem already even without AI.  
**[03:28]** So, functional coverage is more important.  
**[03:32]** Now, if you have a set of functional, now the problem is how will you help a tester  
**[03:37]** based on the change that the developer has done, how will you help a tester to identify  
**[03:42]** the right set of test cases?  
**[03:44]** Okay.  
**[03:45]** Because all this is a black box for tester.  
**[03:48]** He doesn't know what is the, you know, PR number, what is the, you know, change that  
**[03:56]** the developer has done, all that you will not know.  
**[04:01]** What he will see is, you will just see that a message from the developer saying your application,  
**[04:07]** now the changes are like what, you just saw a message now from Shashank.  
**[04:12]** The change is deployed in Dev.  
**[04:17]** Right, so what change is deployed, what, what Joel has done, who knows.  
**[04:22]** We don't know, Shashank doesn't know that.  
**[04:25]** But Shashank, what he knows now is that there is a change deployed and he has to test.  
**[04:29]** And for him, the test is whether for wealth, now is he able to retrieve the change.  
**[04:34]** That is a test he does.  
**[04:36]** But he has no idea on how Pregwell [name unclear] has fixed what he has done and all that.  
**[04:41]** But now if we have this particular tool that you are talking about, right, this particular capability in Pulse.  
**[04:47]** Now ideally, Pulse should tell him that based on whatever code that Pregwell [name unclear] has committed and merged now.  
**[04:58]** Anyway, yes, you have to test for wealth because now he has changed the variable as wealth.  
**[05:04]** But while changing it, he has also made these two changes.  
**[05:08]** Because of which you are, you know, we are other agents in the agent box or you are other methods calling this particular.  
**[05:21]** Because other methods are using a different environment and today Pregwell [name unclear] has introduced a new environment variable.  
**[05:29]** So these are some potential test cases that you have to test.  
**[05:33]** Is the information that this agent should be brought up?  
**[05:41]** Got it, right?  
**[05:42]** Yes, yes, correct.  
**[05:43]** So that's why what you should do is I'll tell you, you take a very sample lab only.  
**[05:49]** We'll do Fineract later.  
**[05:51]** Whatever the sample payments [app] that you took earlier.  
**[05:56]** That is based on the punch and then you also make one change into that.  
**[06:01]** That is your latest feature branch, right?  
**[06:04]** The change that you have done.  
**[06:06]** And then you also take test cases.  
**[06:11]** Okay.  
**[06:12]** Test cases that can already be in your graph or it can also be separately wherever.  
**[06:20]** If it is in the graph, then it is a knowledge fabric demo that you will do.  
**[06:25]** If it is outside, then it becomes an agent demo.  
**[06:27]** Correct.  
**[06:28]** So you do a knowledge fabric demo.  
**[06:31]** You have some test cases also there in your knowledge, in your graph.  
**[06:35]** Okay.  
**[06:36]** So you have some 10 test cases, functional test cases for your payments.  
**[06:41]** Then based on the change, now it should identify like these are the three or four regression  
**[06:47]** test cases you should do.  
**[06:49]** That's the demo that you should do.  
**[06:52]** Sure.  
**[06:53]** One big branch, one base branch, one change that you do.  
**[06:58]** And then that will be a different branch, right?  
**[07:02]** Now we will compare these two branches based on the changes that has happened in the new  
**[07:09]** feature branch.  
**[07:12]** Now it should say that these are the 10 test cases impacted.  
**[07:17]** These are the 10 test cases that you should test as part of the regression.  
**[07:22]** Okay.  
**[07:32]** So here we have function calls test and again.  
**[08:01]** Okay.  
**[08:15]** So every test, whatever the test calls, right, like any function or anything, they will be  
**[08:22]** mapped together.  
**[08:24]** But how will it map because the test cases are part of your functions or functions in  
**[08:34]** Neo4j now?  
**[08:36]** In Neo4j.  
**[08:37]** Yes, we can construct it.  
**[08:40]** How do you want?  
**[08:42]** But we know that for example, there is a wrapper builder, right?  
**[08:46]** So we know that this wrapper builder, where it is coming, what is the file, where it is  
**[08:53]** coming and what are the test cases it is connecting.  
**[09:00]** You built some 10 functional test cases from the UI screen.  
**[09:04]** From UI screens, okay.  
**[09:07]** And those UI test cases, how are you going to map to your change in the code?  
**[09:14]** Okay.  
**[09:22]** So, we are out.  
**[09:24]** That's the demo.  
**[09:25]** Yeah, I have these 10 test cases written.  
**[09:28]** These 10 test cases are already there in my repository.  
**[09:31]** Now there is a change in this particular functionality and I know the feature branch for that.  
**[09:39]** Right.  
**[09:40]** And then based on that change against the base branch, now I have understood.  
**[09:45]** In out of the 10 test cases, these four have to be regression tested.  
**[09:50]** Apart from whatever, of course, for the change anyway, the tester would have created new test cases with pulse.  
**[09:57]** But along with that, you are saying based on the code changes, these are four test cases that needs to be regression tested.  
**[10:06]** Okay.  
**[10:07]** Okay, okay, that's it.  
**[10:13]** Now, then once it will do that, then what it shows is that your use case is solved.  
**[10:19]** Now you are adding value from a use case standpoint that you can find out the difference between the branch and you can also, you know, you are also able to, whenever you see the branch, you can store it in a way it can be traversed, right.  
**[10:41]** It can be used to find out the blast radius.  
**[10:45]** So that is the way you are storing all the information in the graph.  
**[10:50]** Yes.  
**[10:51]** So that anyway, you are establishing.  
**[10:53]** So for this use case, your way of seeing, you are not doing a, you are not changing your backend structure for each of these, I hope you are understanding that.  
**[11:07]** You already decided the knowledge fabric structure now.  
**[11:12]** Yes.  
**[11:13]** [unclear].  
**[11:15]** Yes.  
**[11:16]** So using that, how are you demonstrating is what is your trying to attempt.  
**[11:20]** For each use case, you should not keep changing the way the graph will build, the way it is traversed, all that, you should not keep on changing, right.  
**[11:29]** So that is anyway fixed.  
**[11:30]** Yes, that will be in Neo4j.  
**[11:32]** Yes.  
**[11:33]** Neo4j, we have to be wherever.  
**[11:35]** That is already fixed.  
**[11:36]** Yes.  
**[11:37]** You have fixed it.  
**[11:38]** Now using that, how are you demonstrating the use case is what we are working right now.  
**[11:43]** Correct, correct.  
**[11:44]** Yes.  
**[11:45]** Because I do not know, but you have to keep on changing the way the information is represented in graph for each use case.  
**[11:54]** Makes no sense.  
**[11:56]** Now I do not have access to Neo4j, but because Neo4j had only one instance, but now there are two instance.  
**[12:04]** So now I will load this in the Neo4j.  
**[12:07]** If that is done, these problems may not occur again and again, because once I load, I need to load from the Neo4j.  
**[12:14]** So these things would have been solved that loading every time these issues.  
**[12:19]** Yeah.  
**[12:20]** So is it Neo4j or graph DB [unclear]?  
**[12:22]** Neo4j only.  
**[12:23]** Because we are going to integrate this TIA in [KF] 2.5.  
**[12:27]** Ah, okay.  
**[12:29]** Neo4j only.  
**[12:31]** Yes.  
**[12:32]** So now right now Neo4j already has a new instance.  
**[12:35]** An ontology based graph, you know, a graph that is created based on our current ontology, right?  
**[12:41]** Yes.  
**[12:42]** Domain, product line, function, sub function, requirement, process, rule, everything is already there.  
**[12:49]** Yes.  
**[12:50]** Now for technical impact analysis, now we are creating some graphs based on the code base.  
**[12:55]** How are you relating those two?  
**[12:57]** So before we had, you know, there were options.  
**[13:06]** For example, we extract the rules here that minimum 80 balance or whatever is here, right?  
**[13:15]** They would be connected to rules and those rules would be connected to the rules of the documents.  
**[13:22]** Okay.  
**[13:23]** Then there is also for example, APIs.  
**[13:25]** Here the API endpoints will be connected to the system extracts in 2.5.  
**[13:31]** Okay.  
**[13:32]** Ah, functions.  
**[13:35]** Anyway, it will not, only these two we are trying to bring.  
**[13:40]** Yes.  
**[13:44]** Okay.  
**[13:45]** Okay.  
**[13:46]** So along with that is connected, then you can bring the impact.  
**[13:50]** Because the test cases would again be connected to a sub function and an API would be connected to a sub function.  
**[13:56]** We will be able to find out what are those related test cases and get the conducted regression test.  
**[14:03]** Yes.  
**[14:04]** Okay, fine.  
**[14:06]** Okay.  
**[14:07]** Thank you, Pawan.  
**[14:10]** Yeah, so you do that and then when you regroup tomorrow, will you see that they are going to do,  
**[14:16]** we will all share them also, no?  
**[14:18]** Sure.  
**[14:19]** When he was there, he was invited.  
**[14:21]** Yes, yes.  
**[14:22]** Yeah, I thought he had joined, but I didn't know.  
**[14:24]** He joined actually.  
**[14:26]** Actually, I showed him the demo yesterday.  
**[14:30]** No, not yesterday.  
**[14:32]** Yes, day before yesterday.  
**[14:33]** Okay, those change in fact everything.  
**[14:35]** He asked me the documentation demo, not the code demo.  
**[14:43]** Is that?  
**[14:44]** Yes, because like how, where I am storing, which DB I am going to use, how I am going to extract the data as,  
**[14:51]** how I am creating this graph, so those kind of information.  
**[14:57]** Okay.  
**[14:58]** Yeah.  
**[14:59]** Yeah, that is what, because say that should not change again and again.  
**[15:02]** Yes, correct.  
**[15:03]** That is fixed.  
**[15:05]** Now any use case that we want to talk about, we should see how that your representation helps in achieving that use case.  
**[15:12]** If it is not achieving, then we should see, we should very cautiously make a decision to change that way of representation in the graph.  
**[15:22]** Sure.  
**[15:23]** Right, so that is why, first we fix the representation, it is connected to system extract and API, good.  
**[15:31]** And now for that, what are the various nodes it gets created at also I thought you have.  
**[15:40]** Yes, yes, I can show you sample nodes, it is call kinds, call function, test, these are the things.  
**[15:49]** But it depends, what happens to the backend code, how it is represented versus front end, right.  
**[15:56]** And then, see, this particular class is having four methods and each method is calling some other method of another class.  
**[16:10]** Right, so that is all, that is all extra information for me.  
**[16:16]** You have to take the, you know, what are those, let's say, what are the major classes in your my code base, what are the major, you know, I don't know if you have to go and go to a level of, you know, which method is called where and all that.  
**[16:36]** But you have to, in a very, very, very wrong manner, what I can think of, you know, when I look at the code, I will probably be interested in seeing the execution flow.  
**[16:53]** Or I will be interested in seeing, you know, how my report, what is the, what are the main entities in my report, right, what are those, what is the control, what are those control files, right.  
**[17:08]** I mean, if there are any model views, like that, I'll understand the design and then I'll probably look at the high level view of the code rather than getting into which method is called where, what are those variables.  
**[17:24]** I don't know if it can go on that, no.  
**[17:26]** So you, you, you start that, or you represent in a way, then we'll slightly, slightly see how, because ultimately why we come into this use case mode is so, your representation should help in this use case.  
**[17:40]** Now that this case is stored inside the report, that was the unit cases. Let's see if the test cases are not in the report, what happens, right. It is all connected to your sub function.  
**[17:53]** How will you, how will that help, correct.  
**[17:58]** Correct.  
**[17:59]** And then that is one is impact analysis, and one is automation also. Now I want to see for each object, I want to see what is the down, you know, locator for that.  
**[18:12]** Well, let's say there is a text box, what is the unique identifier for the text box, what is the CSS path or the X path for that text box.  
**[18:22]** I want that to be stored in your graph.  
**[18:26]** Right. So that means I need to store at screens. That means I need to have a node for my application. And for that application, maybe it is broken into multiple screens.  
**[18:36]** So each screen becomes a node. And for under each screen, I have multiple elements. So I need to capture all the elements, each element becomes a node.  
**[18:45]** And under each element, I will have the attribute of what is the how to identify that element when I actually navigate in the application.  
**[18:56]** Because it right. So now for my automation use case, my representation of graph should be like this.  
**[19:03]** I should have application, different pages and under each page, I should have the different objects and the reach object.  
**[19:10]** I should be able to find out the corresponding attributes. Now similarly, now if I have to do, if I have to do, if I have to find out the code diff.  
**[19:24]** Or if I have to do the, understand the changes that the developer has done. So what should be my representation of the graph that you should think.  
**[19:36]** Right. Yes. So let's say there are APIs now. What is the use case from the API we discussed? Let's say my use case is I should be able to very clearly, you know, maybe build the corresponding or rather, you know, multiple test cases for the particular request.  
**[20:05]** Right. In method. So now I will have my API and for under each API, I will have different methods, becomes nodes and the reach method probably I will also have my request body, request and response.  
**[20:29]** And then the request I will store the entire request body is interviewed like that you have to think. So now if I have that tomorrow, there is an API test I need to do.  
**[20:40]** Basically, for that use case, the retriever will be able to quickly go to the API and see what is the method that you're trying to call. For that method, it will be able to get the, go to the request node and find out the request body.  
**[20:55]** And then maybe you will also store the expected response. Sure.  
**[21:04]** And then if there is any other metadata, you can also store that as a node or any, you know, like definitions that are there, API definitions like for instance, you know, this needs to have a customer ID, which is, you know, customer ID of 15 characters, blah, blah, all that, right.  
**[21:27]** Those definitions. So that will help in any use case where that use cases for creation of test cases for that API. So there you can read that structure and deliver the test cases. So all this we are trying to do so that you can think from a use case perspective and decide your structure of how you want to store your nodes.  
**[21:52]** Got it, right?  
**[21:53]** Yes, yes.  
**[21:54]** Now, API and app [UI] store like this, I can get into these use cases of writing test cases, writing, you know, making an API call from pulse itself directly because now I have the request structure.  
**[22:08]** Now, if it is an automation script, if somebody says develop an automation script for this particular flow and application, you know, somebody will give you a flow saying, you know, from screen A to screen B, it can come here and see for this application, do I have screen A details?  
**[22:25]** Yes.  
**[22:26]** Screen B details, yes. What are the objects involved, yes. But how to find out that object on the page, yes, I do have the locator information. It can take out all that information and build a beautiful automation script.  
**[22:39]** That is possible because your graph structure allows all that information to be stored. If you have not stored all that information, then you have automation use case, please.  
**[22:50]** Yes, correct.  
**[22:52]** Okay, to understand how do you structure your data is why we are talking about these use cases.  
**[23:03]** Okay, okay. Yes, yes.  
**[23:05]** Yeah, so now coming back, what you should do is you should have two reports, one in the base and not two reports, two branches. One is the main branch, which is already live. And then now let's say somebody is making a change.  
**[23:18]** The main branch, somebody has pulled and they have their own branch, they have their own branch, various made a change to that. So now for that change, what are the impact of this case? That is the use cases.  
**[23:32]** Now that this case is you take it from your, take it from your nodes that are linked to your sub function or if you still want to show it with your Azure test case only then that also is fine. But that's how you should demonstrate.  
**[23:50]** Okay, Pawan.  
**[23:53]** Okay. Now you are fixed. Now once you are able to do this use case successfully, then that means your structure of your repo is good enough to support a use case where we talk about that change in the application code.  
**[24:09]** We give you the blast radius and the impact test cases that use case is a tick mark based on the way your knowledge is supposedly structured.  
**[24:18]** Now we will go to the next use case for automation. So then when we do these three, four use cases, then your knowledge graph structure should become better.  
**[24:31]** Yes.  
**[24:33]** So that is the intent of this discussions. Yeah, so now that is why I feel I'm asking now tell me how are you storing it? Where are you doing it? What is your database? How are you connecting to the functions of function nodes?  
**[24:47]** All that. So that is, that is the first step if you should do any application code, how are you connecting it is your step zero. And then your step one is your first use case we are talking about blast radius and impact test cases.  
**[25:02]** Then we will go to automation then we will go to API like that.  
**[25:08]** Understood, Pawan.  
**[25:11]** Okay, sir.  
**[25:15]** Okay, I'll tell. Thank you. Thank you Pawan.  
**[25:18]** Thank you, sir. Bye.  
