## Goal:  Get all GitHub Actions/Workflows to complete with "is:sucess"
1. ### Investigate: GitHub Actions - workflow: "Backend CI "
   1. #### **Check: **
       - [ ] the last 2-5 failures of  "Backend CI" action to see what caused each run to have status: "is:failure"
       - [ ] Identify which Jobs are Failing ALways, Sometimes, Never
       - [ ] the last 1-3 runs of "Backend CI" action that have status: "is:skipped"
       - [ ] Identify any runs of  "Backend CI" action that have status "is:Action Required"
   2. #### **Ask:**
       - [ ] What Job or jobs is failing/skipped ?
       - [ ] Was the cause of failure in each case all related?
       - [ ] Are there a number of different issues with the workflow or just a few bugs?
       - [ ] Do the causes of failure in different cases enough that they should be addressed as separate issues?
       - [ ] What prevented each action from being successful?  
2. ### **Reason/Plan**:
   - #### **Organize Findings**
   -  Using the findings**
   - from the runs that were investigated 
   - AND the questions/answers from last section:
3. ### create a table that lists each job you investigated or identified as meeting the criteria as one row:
   
    <!-- Note: Table titles in Markdown can be formatted using headers (###), bold (**), or plain text. 
        The title is simply text placed above the table - not a special Markdown table feature. -->

   ## Table Title:  **Actions:"Backend CI" -  Most Recent Failed Sessions:**
|           Session Name           |  #  | PR# | Status  | Jobs w/ red"X"(Fail) | **What went wrong in ur words** | Fixed | how/why not? |
|:--------------------------------:|:---:|:---:|:-------:|:---------------------|:--------------------------------|:-----:|-------------:|   
| fix: downgrade to python 3.12... | #11 | n/a | failure | Lint(Black + Flake8) | < Reason: ____ >                |  Yes  | Bug : desc . |
|                                  |     |     |         |                      |                                 |       |              |
|                                  |     |     |         |                      |                                 |       |              |

         

