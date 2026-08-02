---
name: Feature request
about: Suggest an idea for this project
title: "[Feature]"
labels: ''
assignees: ''

---

name: "✨ Feature"
description: "새로운 기능 개발"
title: "[Feature] "
labels: ["feature"]
body:
  - type: textarea
    id: summary
    attributes:
      label: 기능 설명
      placeholder: 어떤 기능을 개발하나요?
    validations:
      required: true

  - type: textarea
    id: todo
    attributes:
      label: 작업 내용
      value: |
        - [ ] 
        - [ ] 
    validations:
      required: true
