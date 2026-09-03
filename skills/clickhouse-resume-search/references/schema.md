# Resume Search Schema

## Database

Use `RCW_RC_Voodoo_Jobseeker`. Alias the primary table as `r`.

## Tables And Relevant Columns

### JobSeekerResume

Primary key and output: `ResumeID`, `ResumeGuid`, `JobSeekerID`, `ResumeName`, `JobSeekerName`.

Mandatory status: `DelFlag`, `ResumeState`.

Basic filters: `JobSeekerAge Nullable(UInt8)`, `JobSeekerSex Nullable(UInt8)`, `JobSeekerWorkYear Nullable(UInt8)`, `WorkingState Nullable(Int32)`, `JobSeekerTalentDegree Nullable(Int32)`, `JobSeekerDrivingLicense Nullable(Int32)`, `ISFromSenior Nullable(UInt8)`.

Education: `HighestEducationDegree`, `HighestEducationSpecialty`, `HighestEducationSchool`, `CollegeID`, `CollegeSpecialtyID`.

Career and location: `PresentIndustry`, `PresentCareer`, `ExpectJobType`, `ExpectIndustry1`, `ExpectCareer1`, `ExpectIndustry2`, `ExpectCareer2`, `ExpectIndustry3`, `ExpectCareer3`, `ExpectWorkPlace`, `ExpectWorkPlace1`, `ExpectWorkPlace2`, `ExpectWorkPlace3`, `NewCompanyName`.

Compensation and activity: `ExpectSalary`, `LastLoginTime`, `LastRefreshDate`, `LastEditTime`.

Searchable strings: `ResumeName`, `JobSeekerName`, `HighestEducationSchool`, `ExpectWorkPlace`, `NewCompanyName`, `ResmueCode`.

### JobSeekerResumeExtension

Join by `ResumeID`. Search and output: `SelfEvaluation`, `ExpectOtherAppeal`, `StrongSuit`, `WorkingExperience`, `LanguageInfo`, `Interesting`, `PresentCareerName`, `ExpectCareerName1`, `ExpectCareerName2`, `ExpectCareerName3`, `HighestEducationSpecialtyName`, `ExpectWelfare`, `PresentAnnualSalary`, `ExpectAnnualSalary`, `ExpectAnnualSalary2`, `CurrentSalary`, `ExpectPositionLevel`, `ExpectPositionNature`, `JobSeekerAbroadExperienceInfo`, `AttachmenExperienceInfo`.

### JobSeekerResumeExperience

Join or filter by `ResumeID`. `ExperienceType = 2` means employment. `ExperienceText1` is the organization name and `ExperienceText2` is the position or career label. Other searchable strings: `ExperienceText3` through `ExperienceText9`, `ExperienceLingText`, `EnterpriseIntroduction`, `JobDescription`, `JobPerformance`, `HigherUp`, `CustomKeywords`, `PositionKeywordIds`.

Dates and flags: `ExperienceStartTime`, `ExperienceFinishTime`, `IsToThisDay`, `HideIt`. Use the greatest `ExperienceStartTime`, with `ExperienceID` as the tie breaker, for latest employment.

### JobSeekerResumeProjectExperience

Join or filter by `ResumeID`. Search `ProjectName` and `ProjectDescription`. Dates are `BeginTime` and `EndTime`; `PlayRole` is an undocumented enum.

### JobSeekerBaseInfo

Join by `JobSeekerID` only when a requested attribute is absent from the resume table. Relevant fields include `JobSeekerAge`, `JobSeekerSex`, `JobSeekerWorkYear`, `JobSeekerWorkState`, `JobSeekerDrivingLicense`, and `LastRefreshDate`. Never select `JobSeekerPassword`.

## Numeric Range Semantics

Treat natural-language numeric ranges as inclusive unless the user explicitly excludes an endpoint. For example, "25 to 35 years old" maps to `r.JobSeekerAge >= {min_age:UInt8}` and `r.JobSeekerAge <= {max_age:UInt8}`.

## Summary Query Template

Add parameterized filter clauses to the `WHERE` block after the mandatory predicates and before `QUALIFY`.

```sql
WITH latest_employment AS
(
    SELECT
        ResumeID,
        argMax(
            tuple(ExperienceText1, ExperienceText2, ExperienceStartTime, ExperienceFinishTime, IsToThisDay),
            tuple(ifNull(ExperienceStartTime, toDateTime64('1900-01-01 00:00:00', 3, 'Asia/Shanghai')), ExperienceID)
        ) AS LatestEmployment
    FROM RCW_RC_Voodoo_Jobseeker.JobSeekerResumeExperience
    WHERE ExperienceType = 2
      AND ifNull(HideIt, 0) = 0
    GROUP BY ResumeID
)
SELECT
    toString(r.ResumeGuid) AS ResumeGuid,
    r.ResumeID AS ResumeID,
    r.JobSeekerID AS JobSeekerID,
    r.JobSeekerName,
    r.JobSeekerAge,
    r.JobSeekerSex,
    r.JobSeekerWorkYear,
    r.WorkingState,
    r.HighestEducationDegree,
    r.HighestEducationSchool,
    ext.HighestEducationSpecialtyName,
    ext.PresentCareerName,
    ext.ExpectCareerName1,
    ext.ExpectCareerName2,
    ext.ExpectCareerName3,
    r.ExpectWorkPlace,
    r.ExpectSalary,
    r.LastRefreshDate,
    r.LastEditTime,
    tupleElement(emp.LatestEmployment, 1) AS LatestCompany,
    tupleElement(emp.LatestEmployment, 2) AS LatestPosition,
    tupleElement(emp.LatestEmployment, 3) AS LatestWorkStartTime,
    tupleElement(emp.LatestEmployment, 4) AS LatestWorkFinishTime,
    tupleElement(emp.LatestEmployment, 5) AS LatestIsToThisDay
FROM RCW_RC_Voodoo_Jobseeker.JobSeekerResume AS r
ANY LEFT JOIN RCW_RC_Voodoo_Jobseeker.JobSeekerResumeExtension AS ext
    ON ext.ResumeID = r.ResumeID
LEFT JOIN latest_employment AS emp
    ON emp.ResumeID = r.ResumeID
WHERE r.DelFlag = 0
  AND r.ResumeState = 2
  AND r.ResumeGuid IS NOT NULL
QUALIFY row_number() OVER
(
    PARTITION BY r.ResumeGuid
    ORDER BY r.LastRefreshDate DESC, r.LastEditTime DESC, r.ResumeID DESC
) = 1
ORDER BY r.LastRefreshDate DESC, r.LastEditTime DESC
LIMIT {limit:UInt32}
```

## Priority Keyword Tiers

Do not use a single full-text `OR` across all resume, career, employment, project, and school fields. For an unqualified keyword search, run the summary query once per tier in this exact order. Add the selected tier predicate after the mandatory predicates and before `QUALIFY`, then inspect the executor response:

1. **Expected position name**: `ext.ExpectCareerName1`, `ext.ExpectCareerName2`, `ext.ExpectCareerName3`.
2. **Current position**: `ext.PresentCareerName`.
3. **Employment history**: employment rows (`ExperienceType = 2`, visible rows only) and `ExperienceText1` through `ExperienceText9`, `ExperienceLingText`, `EnterpriseIntroduction`, `JobDescription`, `JobPerformance`, `HigherUp`, and `CustomKeywords`.
4. **Project experience**: `ProjectName` and `ProjectDescription`.
5. **Resume name or school**: `r.ResumeName` and `r.HighestEducationSchool`.

Each keyword must match at least one field in the active tier. Therefore, join keyword clauses with `AND`, and keep that tier's field alternatives inside one parenthesized `OR` expression. If the response `count` is greater than zero, return only that response and stop, even if it contains fewer rows than the requested limit. Query the next tier only after a zero-row response. Never supplement a non-empty higher-priority response with lower-tier rows.

Use these parameterized predicates as templates. For multiple keywords, repeat the relevant block with `{keyword_1:String}`, `{keyword_2:String}`, and so on, joining the repeated blocks with `AND`.

### 1. Expected Position Name

```sql
AND
(
    positionCaseInsensitiveUTF8(ifNull(ext.ExpectCareerName1, ''), {keyword_0:String}) > 0
    OR positionCaseInsensitiveUTF8(ifNull(ext.ExpectCareerName2, ''), {keyword_0:String}) > 0
    OR positionCaseInsensitiveUTF8(ifNull(ext.ExpectCareerName3, ''), {keyword_0:String}) > 0
)
```

### 2. Current Position

```sql
AND positionCaseInsensitiveUTF8(ifNull(ext.PresentCareerName, ''), {keyword_0:String}) > 0
```

### 3. Employment History

```sql
AND r.ResumeID IN
(
    SELECT ResumeID
    FROM RCW_RC_Voodoo_Jobseeker.JobSeekerResumeExperience
    WHERE ExperienceType = 2
      AND ifNull(HideIt, 0) = 0
      AND arrayExists(
          value -> positionCaseInsensitiveUTF8(value, {keyword_0:String}) > 0,
          [
              ifNull(ExperienceText1, ''), ifNull(ExperienceText2, ''),
              ifNull(ExperienceText3, ''), ifNull(ExperienceText4, ''),
              ifNull(ExperienceText5, ''), ifNull(ExperienceText6, ''),
              ifNull(ExperienceText7, ''), ifNull(ExperienceText8, ''),
              ifNull(ExperienceText9, ''), ifNull(ExperienceLingText, ''),
              ifNull(EnterpriseIntroduction, ''), ifNull(JobDescription, ''),
              ifNull(JobPerformance, ''), ifNull(HigherUp, ''),
              ifNull(CustomKeywords, '')
          ]
      )
)
```

### 4. Project Experience

```sql
AND r.ResumeID IN
(
    SELECT ResumeID
    FROM RCW_RC_Voodoo_Jobseeker.JobSeekerResumeProjectExperience
    WHERE positionCaseInsensitiveUTF8(ifNull(ProjectName, ''), {keyword_0:String}) > 0
       OR positionCaseInsensitiveUTF8(ifNull(ProjectDescription, ''), {keyword_0:String}) > 0
)
```

### 5. Resume Name Or School

```sql
AND
(
    positionCaseInsensitiveUTF8(ifNull(r.ResumeName, ''), {keyword_0:String}) > 0
    OR positionCaseInsensitiveUTF8(ifNull(r.HighestEducationSchool, ''), {keyword_0:String}) > 0
)
```

## Enum Rule

The database does not expose verified labels for several integer codes, including education degree, sex, industry, career, location, salary, and project role. Do not infer ordering or labels from numeric values. Use numeric codes only when the user supplies them or when a separately verified mapping is added to this reference.
